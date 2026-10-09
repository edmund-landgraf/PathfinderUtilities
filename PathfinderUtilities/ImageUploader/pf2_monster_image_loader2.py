import io
import os
import sys
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import pyodbc
from PIL import Image, ImageTk, ImageGrab, ImageOps


# ============================================================
# Configuration
# ============================================================
# Environment variables supported:
#   PF2_DB_SERVER
#   PF2_DB_DATABASE
#   PF2_DB_USER
#   PF2_DB_PASSWORD
#   PF2_ODBC_DRIVER
#
# Windows uses Trusted_Connection. Other platforms use PF2_DB_USER / PF2_DB_PASSWORD.
#
DB_SERVER = os.getenv("PF2_DB_SERVER", "localhost")
DB_DATABASE = os.getenv("PF2_DB_DATABASE", "PathfinderUtil")
DB_USER = os.getenv("PF2_DB_USER", "")
DB_PASSWORD = os.getenv("PF2_DB_PASSWORD", "")
ODBC_DRIVER = os.getenv("PF2_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")

FULL_IMAGE_MAX = (1600, 1600)
THUMBNAIL_MAX = (256, 256)

# Load all monsters so existing images can be replaced.  The UI defaults to
# hiding completed monsters unless "Show monsters with images" is checked.
MONSTER_LIST_SQL = """
SELECT
    m.MonsterId,
    m.Name,
    CASE WHEN mi.MonsterID IS NULL THEN CAST(0 AS bit) ELSE CAST(1 AS bit) END AS HasImage
FROM pf2.Monster AS m
LEFT JOIN pf2.MonsterImage AS mi
    ON mi.MonsterID = m.MonsterId
ORDER BY
    CASE WHEN mi.MonsterID IS NULL THEN 0 ELSE 1 END,
    m.Name;
"""

MONSTER_DETAIL_SQL = """
SELECT
    m.MonsterId,
    m.AonId,
    m.AonUrl,
    m.Name,
    m.Level,
    m.RarityId,
    m.SizeId,
    m.AlignmentId,
    m.FamilyId,
    m.SourceBookId,
    m.SourcePage,
    m.IsUnique,
    m.IsNPC,
    m.ImageUrl,
    m.RawText,
    m.RawMD,
    CASE WHEN mi.MonsterID IS NULL THEN CAST(0 AS bit) ELSE CAST(1 AS bit) END AS HasImage
FROM pf2.Monster AS m
LEFT JOIN pf2.MonsterImage AS mi
    ON mi.MonsterID = m.MonsterId
WHERE m.MonsterId = ?;
"""

MONSTER_IMAGE_SQL = """
SELECT MonsterImage
FROM pf2.MonsterImage
WHERE MonsterID = ?;
"""


def build_connection_string():
    parts = [
        f"DRIVER={{{ODBC_DRIVER}}}",
        f"SERVER={DB_SERVER}",
        f"DATABASE={DB_DATABASE}",
        "TrustServerCertificate=yes",
    ]

    if sys.platform == "win32":
        parts.append("Trusted_Connection=yes")
    else:
        parts.extend([
            f"UID={DB_USER or os.environ.get('MSSQL_USER', 'sa')}",
            f"PWD={DB_PASSWORD or os.environ.get('MSSQL_PASSWORD', '')}",
        ])

    return ";".join(parts) + ";"


def image_to_rgb(image):
    """Convert a Pillow image to RGB, compositing transparency on white."""
    image = ImageOps.exif_transpose(image)

    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        return background.convert("RGB")

    return image.convert("RGB")


def encode_jpeg(image, max_size, quality):
    """Resize to fit max_size without enlarging and return JPEG bytes."""
    prepared = image_to_rgb(image)
    prepared.thumbnail(max_size, Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    prepared.save(
        buffer,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=True,
    )
    return buffer.getvalue()


class MonsterImageApp(tk.Tk):
    LOOKUPS = {
        "RarityId": ("pf2.Rarity", "RarityId"),
        "SizeId": ("pf2.SizeCategory", "SizeId"),
        "AlignmentId": ("pf2.Alignment", "AlignmentId"),
        "FamilyId": ("pf2.MonsterFamily", "FamilyId"),
        "SourceBookId": ("pf2.SourceBook", "SourceBookId"),
    }

    def __init__(self):
        super().__init__()

        self.title("PF2 Monster Image Loader")
        self.geometry("1420x860")
        self.minsize(1100, 700)

        self.conn = None
        self.all_monsters = []
        self.current_monster = None
        self.current_image = None
        self.preview_photo = None
        self.lookup_cache = {}

        # True only after the user chooses/pastes a new image.  Existing stored
        # images can be viewed without accidentally enabling Replace.
        self.image_dirty = False
        self.current_image_is_stored = False

        self.search_var = tk.StringVar()
        self.show_existing_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Connecting to PathfinderUtil...")
        self.image_status_var = tk.StringVar(value="No image loaded")

        self._build_ui()
        self._bind_shortcuts()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.connect_and_load)

    # ========================================================
    # UI
    # ========================================================
    def _build_ui(self):
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ---------------- Left: monster grid ----------------
        left = ttk.Frame(self, padding=(10, 10, 5, 10))
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(3, weight=1)
        left.columnconfigure(0, weight=1)

        ttk.Label(
            left,
            text="Monster Images",
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        search_frame = ttk.Frame(left)
        search_frame.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        search_frame.columnconfigure(0, weight=1)

        search_entry = ttk.Entry(
            search_frame,
            textvariable=self.search_var,
            width=34,
        )
        search_entry.grid(row=0, column=0, sticky="ew")
        search_entry.bind("<KeyRelease>", lambda _event: self.apply_filter())

        ttk.Button(
            search_frame,
            text="Refresh",
            command=self.load_monsters,
        ).grid(row=0, column=1, padx=(6, 0))

        ttk.Checkbutton(
            left,
            text="Show monsters with images",
            variable=self.show_existing_var,
            command=self.apply_filter,
        ).grid(row=2, column=0, sticky="w", pady=(0, 7))

        grid_frame = ttk.Frame(left)
        grid_frame.grid(row=3, column=0, sticky="nsew")
        grid_frame.rowconfigure(0, weight=1)
        grid_frame.columnconfigure(0, weight=1)

        self.monster_tree = ttk.Treeview(
            grid_frame,
            columns=("MonsterId", "Name", "ImageStatus"),
            show="headings",
            selectmode="browse",
            height=28,
        )
        self.monster_tree.heading("MonsterId", text="Monster ID")
        self.monster_tree.heading("Name", text="Name")
        self.monster_tree.heading("ImageStatus", text="Image")
        self.monster_tree.column("MonsterId", width=85, anchor="e", stretch=False)
        self.monster_tree.column("Name", width=255, anchor="w", stretch=True)
        self.monster_tree.column("ImageStatus", width=70, anchor="center", stretch=False)
        self.monster_tree.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(
            grid_frame,
            orient="vertical",
            command=self.monster_tree.yview,
        )
        yscroll.grid(row=0, column=1, sticky="ns")
        self.monster_tree.configure(yscrollcommand=yscroll.set)

        # Treeview selection events are sufficient normally.  ButtonRelease is
        # also bound because it makes row/detail updates reliable across Tk builds.
        self.monster_tree.bind("<<TreeviewSelect>>", self.on_monster_selected)
        self.monster_tree.bind("<ButtonRelease-1>", self.on_tree_click, add="+")
        self.monster_tree.bind("<KeyRelease-Up>", self.on_monster_selected, add="+")
        self.monster_tree.bind("<KeyRelease-Down>", self.on_monster_selected, add="+")

        # ---------------- Right: detail + image pane ----------------
        right = ttk.Frame(self, padding=(5, 10, 10, 10))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self.title_label = ttk.Label(
            right,
            text="Select a monster",
            font=("Segoe UI", 16, "bold"),
        )
        self.title_label.grid(row=0, column=0, sticky="w", pady=(0, 8))

        body = ttk.Panedwindow(right, orient=tk.HORIZONTAL)
        body.grid(row=1, column=0, sticky="nsew")

        # Detail panel ------------------------------------------------
        detail_frame = ttk.LabelFrame(body, text="Monster Details", padding=10)
        body.add(detail_frame, weight=1)
        detail_frame.columnconfigure(1, weight=1)
        detail_frame.rowconfigure(15, weight=1)

        self.detail_widgets = {}
        detail_fields = [
            ("MonsterId", "Monster ID"),
            ("Name", "Name"),
            ("Level", "Level"),
            ("RarityId", "Rarity"),
            ("SizeId", "Size"),
            ("AlignmentId", "Alignment"),
            ("FamilyId", "Family"),
            ("SourceBookId", "Source Book"),
            ("SourcePage", "Source Page"),
            ("AonId", "AoN ID"),
            ("IsUnique", "Unique"),
            ("IsNPC", "NPC"),
            ("ImageUrl", "Image URL"),
        ]

        for row, (field, label_text) in enumerate(detail_fields):
            ttk.Label(
                detail_frame,
                text=label_text + ":",
                font=("Segoe UI", 9, "bold"),
            ).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)

            value_label = ttk.Label(
                detail_frame,
                text="—",
                wraplength=370,
                justify="left",
            )
            value_label.grid(row=row, column=1, sticky="nw", pady=2)
            self.detail_widgets[field] = value_label

        self.aon_button = ttk.Button(
            detail_frame,
            text="Open Archives of Nethys",
            command=self.open_aon_url,
            state="disabled",
        )
        self.aon_button.grid(
            row=13,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(8, 8),
        )

        ttk.Label(
            detail_frame,
            text="Monster Description / Text:",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=14, column=0, columnspan=2, sticky="w", pady=(2, 4))

        description_frame = ttk.Frame(detail_frame)
        description_frame.grid(row=15, column=0, columnspan=2, sticky="nsew")
        description_frame.rowconfigure(0, weight=1)
        description_frame.columnconfigure(0, weight=1)

        self.description_text = tk.Text(
            description_frame,
            wrap="word",
            height=14,
            width=48,
            font=("Segoe UI", 9),
            relief="solid",
            borderwidth=1,
            padx=7,
            pady=7,
        )
        self.description_text.grid(row=0, column=0, sticky="nsew")

        description_scroll = ttk.Scrollbar(
            description_frame,
            orient="vertical",
            command=self.description_text.yview,
        )
        description_scroll.grid(row=0, column=1, sticky="ns")
        self.description_text.configure(yscrollcommand=description_scroll.set)
        self.description_text.configure(state="disabled")

        # Image panel -------------------------------------------------
        image_frame = ttk.LabelFrame(body, text="Monster Image", padding=10)
        body.add(image_frame, weight=2)
        image_frame.columnconfigure(0, weight=1)
        image_frame.columnconfigure(1, weight=1)
        image_frame.columnconfigure(2, weight=1)
        image_frame.rowconfigure(0, weight=1)

        self.preview_canvas = tk.Canvas(
            image_frame,
            width=580,
            height=560,
            background="#f2f2f2",
            highlightthickness=1,
            highlightbackground="#999999",
        )
        self.preview_canvas.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self.preview_canvas.bind("<Button-1>", lambda _event: self.choose_image())
        self.preview_canvas.bind("<Configure>", lambda _event: self.render_preview())

        ttk.Label(
            image_frame,
            textvariable=self.image_status_var,
            wraplength=650,
            justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 6))

        ttk.Button(
            image_frame,
            text="Choose Image...",
            command=self.choose_image,
        ).grid(row=2, column=0, sticky="ew", padx=(0, 4))

        ttk.Button(
            image_frame,
            text="Paste Clipboard",
            command=self.paste_from_clipboard,
        ).grid(row=2, column=1, sticky="ew", padx=4)

        self.save_button = ttk.Button(
            image_frame,
            text="Save Image",
            command=self.save_or_replace_image,
            state="disabled",
        )
        self.save_button.grid(row=2, column=2, sticky="ew", padx=(4, 0))

        # Status bar --------------------------------------------------
        status = ttk.Label(
            right,
            textvariable=self.status_var,
            anchor="w",
            relief="sunken",
            padding=(6, 4),
        )
        status.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        self.render_preview()

    def _bind_shortcuts(self):
        self.bind_all("<Control-v>", self._ctrl_v)
        self.bind_all("<Control-V>", self._ctrl_v)

    def _ctrl_v(self, _event=None):
        focused = self.focus_get()

        # Allow ordinary text paste in search.  Description is read-only, so
        # Ctrl+V elsewhere is treated as an image paste.
        if isinstance(focused, (tk.Entry, ttk.Entry)):
            return None

        self.paste_from_clipboard()
        return "break"

    # ========================================================
    # Database
    # ========================================================
    def connect_and_load(self):
        try:
            self.conn = pyodbc.connect(build_connection_string(), timeout=8)
            self.status_var.set(f"Connected to {DB_SERVER} / {DB_DATABASE}")
            self.load_monsters()
        except Exception as exc:
            self.status_var.set("Database connection failed.")
            messagebox.showerror(
                "Database Connection Failed",
                "Could not connect to SQL Server.\n\n"
                f"{exc}\n\n"
                "Check DB_SERVER / DB_DATABASE / credentials / ODBC driver "
                "at the top of the script or via PF2_DB_* environment variables.",
            )

    def ensure_connection(self):
        if self.conn is None:
            raise RuntimeError("No database connection is available.")

        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
        except Exception:
            self.conn = pyodbc.connect(build_connection_string(), timeout=8)

    def load_monsters(self, preserve_monster_id=None):
        try:
            self.ensure_connection()
            cursor = self.conn.cursor()
            cursor.execute(MONSTER_LIST_SQL)

            self.all_monsters = [
                {
                    "MonsterId": int(row.MonsterId),
                    "Name": row.Name,
                    "HasImage": bool(row.HasImage),
                }
                for row in cursor.fetchall()
            ]

            self.apply_filter(preserve_monster_id=preserve_monster_id)

            missing = sum(1 for row in self.all_monsters if not row["HasImage"])
            existing = len(self.all_monsters) - missing
            self.status_var.set(
                f"{missing:,} monster(s) missing images; "
                f"{existing:,} monster(s) already have images."
            )

        except Exception as exc:
            messagebox.showerror(
                "Load Failed",
                f"Could not load monsters.\n\n{exc}",
            )

    def apply_filter(self, preserve_monster_id=None):
        term = self.search_var.get().strip().lower()
        show_existing = self.show_existing_var.get()

        if preserve_monster_id is None:
            selection = self.monster_tree.selection()
            if selection:
                values = self.monster_tree.item(selection[0], "values")
                if values:
                    try:
                        preserve_monster_id = int(values[0])
                    except Exception:
                        preserve_monster_id = None

        for item in self.monster_tree.get_children():
            self.monster_tree.delete(item)

        filtered = [
            row
            for row in self.all_monsters
            if (show_existing or not row["HasImage"])
            and (not term or term in row["Name"].lower())
        ]

        for row in filtered:
            self.monster_tree.insert(
                "",
                "end",
                iid=f"monster-{row['MonsterId']}",
                values=(
                    row["MonsterId"],
                    row["Name"],
                    "Yes" if row["HasImage"] else "No",
                ),
            )

        # Restore selection when possible, otherwise select first visible row.
        target_iid = None
        if preserve_monster_id is not None:
            possible = f"monster-{preserve_monster_id}"
            if self.monster_tree.exists(possible):
                target_iid = possible

        if target_iid is None:
            children = self.monster_tree.get_children()
            if children:
                target_iid = children[0]

        if target_iid:
            self.monster_tree.selection_set(target_iid)
            self.monster_tree.focus(target_iid)
            self.monster_tree.see(target_iid)
            values = self.monster_tree.item(target_iid, "values")
            if values:
                self.show_monster(int(values[0]))
        else:
            self.clear_details()

    def on_tree_click(self, event):
        row_id = self.monster_tree.identify_row(event.y)
        if not row_id:
            return
        self.monster_tree.selection_set(row_id)
        self.monster_tree.focus(row_id)
        values = self.monster_tree.item(row_id, "values")
        if values:
            self.show_monster(int(values[0]))

    def on_monster_selected(self, _event=None):
        selection = self.monster_tree.selection()
        if not selection:
            return

        values = self.monster_tree.item(selection[0], "values")
        if values:
            self.show_monster(int(values[0]))

    def fetch_monster(self, monster_id):
        self.ensure_connection()
        cursor = self.conn.cursor()
        cursor.execute(MONSTER_DETAIL_SQL, monster_id)
        row = cursor.fetchone()

        if row is None:
            return None

        columns = [column[0] for column in cursor.description]
        return dict(zip(columns, row))

    def fetch_stored_image(self, monster_id):
        self.ensure_connection()
        cursor = self.conn.cursor()
        cursor.execute(MONSTER_IMAGE_SQL, monster_id)
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return None

        raw = bytes(row[0])
        with Image.open(io.BytesIO(raw)) as img:
            return ImageOps.exif_transpose(img).copy()

    def lookup_name(self, field, value):
        if value is None or field not in self.LOOKUPS:
            return None

        cache_key = (field, value)
        if cache_key in self.lookup_cache:
            return self.lookup_cache[cache_key]

        table, id_column = self.LOOKUPS[field]
        sql = f"SELECT TOP (1) * FROM {table} WHERE {id_column} = ?"

        try:
            cursor = self.conn.cursor()
            cursor.execute(sql, value)
            row = cursor.fetchone()
            if row is None:
                return None

            columns = [column[0] for column in cursor.description]
            row_dict = dict(zip(columns, row))

            preferred_names = [
                "Name",
                "DisplayName",
                "Title",
                "Rarity",
                "Size",
                "Alignment",
                "FamilyName",
                "Family",
                "BookName",
                "SourceBook",
            ]

            for preferred in preferred_names:
                for column_name, column_value in row_dict.items():
                    if (
                        column_name.lower() == preferred.lower()
                        and column_value not in (None, "")
                    ):
                        result = str(column_value)
                        self.lookup_cache[cache_key] = result
                        return result

            for column_name, column_value in row_dict.items():
                if column_name.lower() == id_column.lower():
                    continue
                if isinstance(column_value, str) and column_value.strip():
                    result = column_value.strip()
                    self.lookup_cache[cache_key] = result
                    return result

        except Exception:
            pass

        return None

    # ========================================================
    # Monster detail
    # ========================================================
    def show_monster(self, monster_id):
        try:
            monster = self.fetch_monster(monster_id)
            if monster is None:
                messagebox.showwarning(
                    "Monster Not Found",
                    f"Monster ID {monster_id} no longer exists.",
                )
                self.load_monsters()
                return

            self.current_monster = monster
            self.current_image = None
            self.preview_photo = None
            self.image_dirty = False
            self.current_image_is_stored = False

            name = monster.get("Name") or f"Monster {monster_id}"
            self.title_label.config(text=name)

            for field, widget in self.detail_widgets.items():
                value = monster.get(field)

                if field in self.LOOKUPS and value is not None:
                    lookup = self.lookup_name(field, value)
                    value = f"{lookup} ({value})" if lookup else value
                elif field in ("IsUnique", "IsNPC"):
                    if value is None:
                        value = "—"
                    else:
                        value = "Yes" if bool(value) else "No"
                elif value in (None, ""):
                    value = "—"

                widget.config(text=str(value))

            # RawText is the most readable plain-text source.  RawMD is used as
            # a fallback if RawText is empty.
            description = monster.get("RawText") or monster.get("RawMD") or ""
            self.set_description(description)

            aon_url = monster.get("AonUrl")
            self.aon_button.config(state="normal" if aon_url else "disabled")

            has_image = bool(monster.get("HasImage"))
            if has_image:
                try:
                    self.current_image = self.fetch_stored_image(monster_id)
                    self.current_image_is_stored = self.current_image is not None
                except Exception as image_exc:
                    self.current_image = None
                    self.current_image_is_stored = False
                    self.image_status_var.set(
                        f"Monster has an image row, but the stored image could not be read: {image_exc}"
                    )

                self.save_button.config(text="Replace Image", state="disabled")
                if self.current_image is not None:
                    self.image_status_var.set(
                        "Stored image shown. Choose or paste a new image to enable Replace Image."
                    )
            else:
                self.save_button.config(text="Save Image", state="disabled")
                self.image_status_var.set(
                    "No image stored — click the rectangle, choose a file, "
                    "or press Ctrl+V with an image on the clipboard."
                )

            self.render_preview()

        except Exception as exc:
            self.status_var.set(f"Could not load Monster ID {monster_id}.")
            messagebox.showerror(
                "Detail Load Failed",
                f"Could not load monster details for Monster ID {monster_id}.\n\n{exc}",
            )

    def set_description(self, text):
        self.description_text.configure(state="normal")
        self.description_text.delete("1.0", "end")
        if text:
            self.description_text.insert("1.0", str(text))
        else:
            self.description_text.insert("1.0", "No RawText or RawMD is stored for this monster.")
        self.description_text.see("1.0")
        self.description_text.configure(state="disabled")

    def clear_details(self):
        self.current_monster = None
        self.current_image = None
        self.preview_photo = None
        self.image_dirty = False
        self.current_image_is_stored = False
        self.title_label.config(text="No monsters to display")

        for widget in self.detail_widgets.values():
            widget.config(text="—")

        self.set_description("")
        self.aon_button.config(state="disabled")
        self.save_button.config(text="Save Image", state="disabled")
        self.image_status_var.set("No image loaded")
        self.render_preview()

    def open_aon_url(self):
        if self.current_monster and self.current_monster.get("AonUrl"):
            webbrowser.open(self.current_monster["AonUrl"])

    # ========================================================
    # Image handling
    # ========================================================
    def choose_image(self):
        if self.current_monster is None:
            messagebox.showinfo(
                "Select a Monster",
                "Select a monster before loading an image.",
            )
            return

        path = filedialog.askopenfilename(
            title="Choose Monster Image",
            filetypes=[
                ("Image files", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff"),
                ("PNG", "*.png"),
                ("JPEG", "*.jpg *.jpeg"),
                ("WebP", "*.webp"),
                ("All files", "*.*"),
            ],
        )

        if not path:
            return

        try:
            with Image.open(path) as img:
                loaded = ImageOps.exif_transpose(img).copy()

            self.set_current_image(
                loaded,
                f"New image: {os.path.basename(path)} — {loaded.width}×{loaded.height}",
            )

        except Exception as exc:
            messagebox.showerror(
                "Image Load Failed",
                f"Could not load that image.\n\n{exc}",
            )

    def paste_from_clipboard(self):
        if self.current_monster is None:
            messagebox.showinfo(
                "Select a Monster",
                "Select a monster before pasting an image.",
            )
            return

        try:
            clip = ImageGrab.grabclipboard()

            if isinstance(clip, Image.Image):
                image = ImageOps.exif_transpose(clip).copy()
                self.set_current_image(
                    image,
                    f"New clipboard image — {image.width}×{image.height}",
                )
                return

            if isinstance(clip, list):
                for path in clip:
                    try:
                        with Image.open(path) as img:
                            image = ImageOps.exif_transpose(img).copy()
                        self.set_current_image(
                            image,
                            f"New image: {os.path.basename(path)} — {image.width}×{image.height}",
                        )
                        return
                    except Exception:
                        continue

            messagebox.showinfo(
                "No Clipboard Image",
                "The clipboard does not currently contain an image.\n\n"
                "Copy an image or screenshot, then press Ctrl+V again.",
            )

        except Exception as exc:
            messagebox.showerror(
                "Clipboard Paste Failed",
                f"Could not read an image from the clipboard.\n\n{exc}",
            )

    def set_current_image(self, image, description):
        self.current_image = image
        self.image_dirty = True
        self.current_image_is_stored = False
        self.image_status_var.set(description)

        if self.current_monster and bool(self.current_monster.get("HasImage")):
            self.save_button.config(text="Replace Image", state="normal")
        else:
            self.save_button.config(text="Save Image", state="normal")

        self.render_preview()

    def render_preview(self):
        canvas = self.preview_canvas
        canvas.delete("all")

        width = max(canvas.winfo_width(), 200)
        height = max(canvas.winfo_height(), 200)

        if self.current_image is None:
            canvas.create_rectangle(
                16,
                16,
                width - 16,
                height - 16,
                outline="#a0a0a0",
                dash=(6, 4),
                width=2,
            )
            canvas.create_text(
                width / 2,
                height / 2 - 18,
                text="Paste image here",
                font=("Segoe UI", 18, "bold"),
                fill="#666666",
            )
            canvas.create_text(
                width / 2,
                height / 2 + 18,
                text="Ctrl+V  •  or click to choose a file",
                font=("Segoe UI", 11),
                fill="#777777",
            )
            return

        preview = ImageOps.exif_transpose(self.current_image).copy()
        preview.thumbnail(
            (max(width - 24, 1), max(height - 24, 1)),
            Image.Resampling.LANCZOS,
        )

        self.preview_photo = ImageTk.PhotoImage(preview)
        canvas.create_image(
            width / 2,
            height / 2,
            image=self.preview_photo,
            anchor="center",
        )

        if self.current_image_is_stored and not self.image_dirty:
            canvas.create_text(
                14,
                14,
                text="CURRENT STORED IMAGE",
                anchor="nw",
                font=("Segoe UI", 9, "bold"),
                fill="#333333",
            )

    # ========================================================
    # Save / replace
    # ========================================================
    def save_or_replace_image(self):
        if self.current_monster is None or self.current_image is None or not self.image_dirty:
            return

        monster_id = int(self.current_monster["MonsterId"])
        monster_name = self.current_monster.get("Name", str(monster_id))

        try:
            self.ensure_connection()
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM pf2.MonsterImage WHERE MonsterID = ?",
                monster_id,
            )
            already_exists = cursor.fetchone()[0] > 0

            if already_exists:
                confirmed = messagebox.askyesno(
                    "Replace Monster Image?",
                    f"{monster_name} already has an image.\n\n"
                    "Replace the existing full image and thumbnail with the new image?",
                    icon="warning",
                    default="no",
                )
                if not confirmed:
                    return

            self.status_var.set(
                f"{'Replacing' if already_exists else 'Saving'} image for {monster_name}..."
            )
            self.update_idletasks()

            full_bytes = encode_jpeg(
                self.current_image,
                FULL_IMAGE_MAX,
                quality=90,
            )
            thumb_bytes = encode_jpeg(
                self.current_image,
                THUMBNAIL_MAX,
                quality=82,
            )

            if already_exists:
                cursor.execute(
                    """
                    UPDATE pf2.MonsterImage
                    SET
                        MonsterImage = ?,
                        MonsterThumbnail = ?
                    WHERE MonsterID = ?;
                    """,
                    pyodbc.Binary(full_bytes),
                    pyodbc.Binary(thumb_bytes),
                    monster_id,
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO pf2.MonsterImage
                        (MonsterID, MonsterImage, MonsterThumbnail)
                    VALUES
                        (?, ?, ?);
                    """,
                    monster_id,
                    pyodbc.Binary(full_bytes),
                    pyodbc.Binary(thumb_bytes),
                )

            self.conn.commit()

            action = "Replaced" if already_exists else "Saved"
            self.status_var.set(
                f"{action} {monster_name}: "
                f"{len(full_bytes) / 1024:.1f} KB full image, "
                f"{len(thumb_bytes) / 1024:.1f} KB thumbnail."
            )

            # Refresh so the image-status column and missing-only filter are accurate.
            # If completed monsters are hidden, a newly saved monster disappears.
            self.load_monsters(
                preserve_monster_id=monster_id if self.show_existing_var.get() else None
            )

        except Exception as exc:
            if self.conn is not None:
                try:
                    self.conn.rollback()
                except Exception:
                    pass

            messagebox.showerror(
                "Save Failed",
                f"Could not save the image for {monster_name}.\n\n{exc}",
            )
            self.status_var.set("Image save failed.")

    def on_close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    try:
        app = MonsterImageApp()
        app.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)
