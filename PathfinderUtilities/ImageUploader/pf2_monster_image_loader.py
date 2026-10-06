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
# You can either edit these defaults or set environment variables:
#
#   PF2_DB_SERVER
#   PF2_DB_DATABASE
#   PF2_DB_USER
#   PF2_DB_PASSWORD
#   PF2_ODBC_DRIVER
#
# Example PowerShell:
#   $env:PF2_DB_SERVER="your-sql-server,1433"
#   $env:PF2_DB_DATABASE="PathfinderUtil"
#   $env:PF2_DB_USER="sa"
#   $env:PF2_DB_PASSWORD="your-password"
#
# If PF2_DB_USER is blank, the app uses Windows Trusted_Connection.
#
DB_SERVER = os.getenv("PF2_DB_SERVER", "localhost")
DB_DATABASE = os.getenv("PF2_DB_DATABASE", "PathfinderUtil")
DB_USER = os.getenv("PF2_DB_USER", "")
DB_PASSWORD = os.getenv("PF2_DB_PASSWORD", "")
ODBC_DRIVER = os.getenv("PF2_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")

FULL_IMAGE_MAX = (1600, 1600)
THUMBNAIL_MAX = (256, 256)

MISSING_MONSTERS_SQL = """
SELECT
    MonsterId,
    Name
FROM pf2.Monster
WHERE MonsterId NOT IN (
    SELECT MonsterID
    FROM pf2.MonsterImage
)
ORDER BY Name;
"""

MONSTER_DETAIL_SQL = """
SELECT
    MonsterId,
    AonId,
    AonUrl,
    Name,
    Level,
    RarityId,
    SizeId,
    AlignmentId,
    FamilyId,
    SourceBookId,
    SourcePage,
    IsUnique,
    IsNPC,
    ImageUrl
FROM pf2.Monster
WHERE MonsterId = ?;
"""


def build_connection_string():
    parts = [
        f"DRIVER={{{ODBC_DRIVER}}}",
        f"SERVER={DB_SERVER}",
        f"DATABASE={DB_DATABASE}",
        "TrustServerCertificate=yes",
    ]

    if DB_USER:
        parts.extend([
            f"UID={DB_USER}",
            f"PWD={DB_PASSWORD}",
        ])
    else:
        parts.append("Trusted_Connection=yes")

    return ";".join(parts) + ";"


def image_to_rgb(image):
    """Convert Pillow image to RGB, compositing transparency on white."""
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
    """Resize to fit max_size without enlarging, then return JPEG bytes."""
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
        self.geometry("1220x760")
        self.minsize(980, 640)

        self.conn = None
        self.all_monsters = []
        self.current_monster = None
        self.current_image = None
        self.preview_photo = None
        self.lookup_cache = {}

        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Connecting to PathfinderUtil...")
        self.image_status_var = tk.StringVar(value="No image loaded")

        self._build_ui()
        self._bind_shortcuts()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # Connect after the window exists so connection errors can be shown cleanly.
        self.after(100, self.connect_and_load)

    # ========================================================
    # UI
    # ========================================================
    def _build_ui(self):
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # ---------------- Left: missing monsters grid ----------------
        left = ttk.Frame(self, padding=(10, 10, 5, 10))
        left.grid(row=0, column=0, sticky="nsew")
        left.rowconfigure(2, weight=1)
        left.columnconfigure(0, weight=1)

        ttk.Label(
            left,
            text="Monsters Missing Images",
            font=("Segoe UI", 13, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        search_frame = ttk.Frame(left)
        search_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        search_frame.columnconfigure(0, weight=1)

        search_entry = ttk.Entry(
            search_frame,
            textvariable=self.search_var,
            width=32,
        )
        search_entry.grid(row=0, column=0, sticky="ew")
        search_entry.bind("<KeyRelease>", lambda _event: self.apply_filter())

        ttk.Button(
            search_frame,
            text="Refresh",
            command=self.load_missing_monsters,
        ).grid(row=0, column=1, padx=(6, 0))

        grid_frame = ttk.Frame(left)
        grid_frame.grid(row=2, column=0, sticky="nsew")
        grid_frame.rowconfigure(0, weight=1)
        grid_frame.columnconfigure(0, weight=1)

        self.monster_tree = ttk.Treeview(
            grid_frame,
            columns=("MonsterId", "Name"),
            show="headings",
            selectmode="browse",
            height=25,
        )
        self.monster_tree.heading("MonsterId", text="Monster ID")
        self.monster_tree.heading("Name", text="Name")
        self.monster_tree.column("MonsterId", width=90, anchor="e", stretch=False)
        self.monster_tree.column("Name", width=280, anchor="w", stretch=True)
        self.monster_tree.grid(row=0, column=0, sticky="nsew")

        yscroll = ttk.Scrollbar(
            grid_frame,
            orient="vertical",
            command=self.monster_tree.yview,
        )
        yscroll.grid(row=0, column=1, sticky="ns")
        self.monster_tree.configure(yscrollcommand=yscroll.set)
        self.monster_tree.bind("<<TreeviewSelect>>", self.on_monster_selected)

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

        # Detail panel
        detail_frame = ttk.LabelFrame(body, text="Monster Details", padding=10)
        body.add(detail_frame, weight=1)
        detail_frame.columnconfigure(1, weight=1)

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
            ).grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=3)

            value_label = ttk.Label(
                detail_frame,
                text="—",
                wraplength=330,
                justify="left",
            )
            value_label.grid(row=row, column=1, sticky="nw", pady=3)
            self.detail_widgets[field] = value_label

        self.aon_button = ttk.Button(
            detail_frame,
            text="Open Archives of Nethys",
            command=self.open_aon_url,
            state="disabled",
        )
        self.aon_button.grid(
            row=len(detail_fields),
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(12, 0),
        )

        # Image panel
        image_frame = ttk.LabelFrame(body, text="Monster Image", padding=10)
        body.add(image_frame, weight=2)
        image_frame.columnconfigure(0, weight=1)
        image_frame.rowconfigure(0, weight=1)

        self.preview_canvas = tk.Canvas(
            image_frame,
            width=500,
            height=500,
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
            text="Save to MonsterImage",
            command=self.save_image,
            state="disabled",
        )
        self.save_button.grid(row=2, column=2, sticky="ew", padx=(4, 0))

        # Status bar
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
        # Paste an image copied from browser, image editor, screenshot tool, etc.
        self.bind_all("<Control-v>", self._ctrl_v)
        self.bind_all("<Control-V>", self._ctrl_v)

    def _ctrl_v(self, _event=None):
        # If focus is in the search Entry, allow normal text paste there.
        focused = self.focus_get()
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
            self.status_var.set(
                f"Connected to {DB_SERVER} / {DB_DATABASE}"
            )
            self.load_missing_monsters()
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

    def load_missing_monsters(self):
        try:
            self.ensure_connection()
            cursor = self.conn.cursor()
            cursor.execute(MISSING_MONSTERS_SQL)

            self.all_monsters = [
                {
                    "MonsterId": row.MonsterId,
                    "Name": row.Name,
                }
                for row in cursor.fetchall()
            ]

            self.apply_filter()

            self.status_var.set(
                f"{len(self.all_monsters):,} monster(s) currently have no image."
            )

            if self.monster_tree.get_children():
                first = self.monster_tree.get_children()[0]
                self.monster_tree.selection_set(first)
                self.monster_tree.focus(first)
                self.monster_tree.see(first)
                self.show_monster(int(self.monster_tree.item(first, "values")[0]))
            else:
                self.clear_details()

        except Exception as exc:
            messagebox.showerror(
                "Load Failed",
                f"Could not load monsters missing images.\n\n{exc}",
            )

    def apply_filter(self):
        term = self.search_var.get().strip().lower()

        selected_id = None
        selection = self.monster_tree.selection()
        if selection:
            values = self.monster_tree.item(selection[0], "values")
            if values:
                selected_id = str(values[0])

        for item in self.monster_tree.get_children():
            self.monster_tree.delete(item)

        filtered = [
            row
            for row in self.all_monsters
            if not term or term in row["Name"].lower()
        ]

        for row in filtered:
            self.monster_tree.insert(
                "",
                "end",
                iid=f"monster-{row['MonsterId']}",
                values=(row["MonsterId"], row["Name"]),
            )

        if selected_id:
            iid = f"monster-{selected_id}"
            if self.monster_tree.exists(iid):
                self.monster_tree.selection_set(iid)
                self.monster_tree.focus(iid)

    def on_monster_selected(self, _event=None):
        selection = self.monster_tree.selection()
        if not selection:
            return

        values = self.monster_tree.item(selection[0], "values")
        if not values:
            return

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

    def lookup_name(self, field, value):
        """
        Resolve foreign-key IDs without assuming the descriptive column name.

        It SELECTs the matching lookup row and chooses the first useful
        non-ID string column. If lookup schema differs, it safely falls
        back to displaying the raw ID.
        """
        if value is None or field not in self.LOOKUPS:
            return None

        cache_key = (field, value)
        if cache_key in self.lookup_cache:
            return self.lookup_cache[cache_key]

        table, id_column = self.LOOKUPS[field]

        # table/id_column come only from the hard-coded allowlist above.
        sql = f"SELECT TOP (1) * FROM {table} WHERE {id_column} = ?"

        try:
            cursor = self.conn.cursor()
            cursor.execute(sql, value)
            row = cursor.fetchone()
            if row is None:
                return None

            columns = [column[0] for column in cursor.description]

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

            row_dict = dict(zip(columns, row))

            for preferred in preferred_names:
                for column_name, column_value in row_dict.items():
                    if (
                        column_name.lower() == preferred.lower()
                        and column_value not in (None, "")
                    ):
                        result = str(column_value)
                        self.lookup_cache[cache_key] = result
                        return result

            # Fallback: first non-ID text value.
            for column_name, column_value in row_dict.items():
                if column_name.lower() == id_column.lower():
                    continue
                if isinstance(column_value, str) and column_value.strip():
                    result = column_value.strip()
                    self.lookup_cache[cache_key] = result
                    return result

        except Exception:
            # A lookup failure should never stop image entry.
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
                self.load_missing_monsters()
                return

            self.current_monster = monster
            self.current_image = None
            self.preview_photo = None

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

            aon_url = monster.get("AonUrl")
            self.aon_button.config(
                state="normal" if aon_url else "disabled"
            )

            self.image_status_var.set(
                "No image loaded — click the rectangle, choose a file, "
                "or press Ctrl+V with an image on the clipboard."
            )
            self.save_button.config(state="disabled")
            self.render_preview()

        except Exception as exc:
            messagebox.showerror(
                "Detail Load Failed",
                f"Could not load monster details.\n\n{exc}",
            )

    def clear_details(self):
        self.current_monster = None
        self.current_image = None
        self.preview_photo = None
        self.title_label.config(text="No monsters missing images")

        for widget in self.detail_widgets.values():
            widget.config(text="—")

        self.aon_button.config(state="disabled")
        self.save_button.config(state="disabled")
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
                f"{os.path.basename(path)} — "
                f"{loaded.width}×{loaded.height}"
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
                    f"Clipboard image — {image.width}×{image.height}"
                )
                return

            # On Windows, ImageGrab can return a list of copied file paths.
            if isinstance(clip, list):
                for path in clip:
                    try:
                        with Image.open(path) as img:
                            image = ImageOps.exif_transpose(img).copy()
                        self.set_current_image(
                            image,
                            f"{os.path.basename(path)} — "
                            f"{image.width}×{image.height}"
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
        self.image_status_var.set(description)
        self.save_button.config(state="normal")
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

    # ========================================================
    # Save
    # ========================================================
    def save_image(self):
        if self.current_monster is None or self.current_image is None:
            return

        monster_id = int(self.current_monster["MonsterId"])
        monster_name = self.current_monster.get("Name", str(monster_id))

        try:
            self.status_var.set(f"Encoding image for {monster_name}...")
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

            self.ensure_connection()
            cursor = self.conn.cursor()

            # Re-check immediately before INSERT so two users/processes
            # cannot accidentally create a duplicate PK row silently.
            cursor.execute(
                "SELECT COUNT(*) FROM pf2.MonsterImage WHERE MonsterID = ?",
                monster_id,
            )
            already_exists = cursor.fetchone()[0] > 0

            if already_exists:
                self.conn.rollback()
                messagebox.showwarning(
                    "Image Already Exists",
                    f"{monster_name} already has a MonsterImage row. "
                    "The list will be refreshed.",
                )
                self.load_missing_monsters()
                return

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

            self.status_var.set(
                f"Saved {monster_name}: "
                f"{len(full_bytes) / 1024:.1f} KB full image, "
                f"{len(thumb_bytes) / 1024:.1f} KB thumbnail."
            )

            self.remove_saved_monster_and_select_next(monster_id)

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

    def remove_saved_monster_and_select_next(self, monster_id):
        self.all_monsters = [
            row
            for row in self.all_monsters
            if row["MonsterId"] != monster_id
        ]

        self.apply_filter()

        children = self.monster_tree.get_children()
        if children:
            first = children[0]
            self.monster_tree.selection_set(first)
            self.monster_tree.focus(first)
            self.monster_tree.see(first)
            values = self.monster_tree.item(first, "values")
            self.show_monster(int(values[0]))
        else:
            self.clear_details()

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
