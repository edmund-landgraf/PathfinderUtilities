#!/usr/bin/env python3
"""
Adventure Module Generator for AdventureMakerByAct
Converts adventure_data.json to valid schema v1.0 export JSON
"""

import json
import uuid
from datetime import datetime
from pathlib import Path

def new_uuid():
    return str(uuid.uuid4())

def load_adventure_data(filepath="adventure_data.json"):
    """Load the adventure content data"""
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

def build_narratives(data):
    """Build module-level narratives from data"""
    narratives = []
    sort_order = 0
    
    for key, narrative in data.get("narratives", {}).items():
        narratives.append({
            "id": new_uuid(),
            "title": narrative["title"],
            "format": narrative["format"],
            "content": narrative["content"],
            "sortOrder": sort_order
        })
        sort_order += 1
    
    return narratives

def build_artifacts(data, module_id):
    """Build module-level artifacts from data"""
    artifacts = []
    sort_order = 0
    
    for key, artifact in data.get("artifacts", {}).items():
        artifacts.append({
            "id": new_uuid(),
            "type": artifact["type"],
            "title": artifact["title"],
            "payload": {
                "format": "html",
                "content": artifact["content"]
            },
            "sortOrder": sort_order
        })
        sort_order += 1
    
    return artifacts

def build_encounter_artifacts(encounter, module_id):
    """Build artifacts for a single encounter"""
    artifacts = []
    sort_order = 0
    
    # Add monster blocks
    for monster in encounter.get("monsters", []):
        artifacts.append({
            "id": new_uuid(),
            "type": "monster_block",
            "title": monster["name"],
            "payload": {
                "format": "html",
                "content": monster["statblock"]
            },
            "sortOrder": sort_order
        })
        sort_order += 1
    
    # Add trap if present
    if "trap" in encounter:
        artifacts.append({
            "id": new_uuid(),
            "type": "trap",
            "title": encounter["trap"]["name"],
            "payload": {
                "format": "html",
                "content": encounter["trap"]["content"]
            },
            "sortOrder": sort_order
        })
        sort_order += 1
    
    # Add treasure if present
    if "treasure" in encounter:
        artifacts.append({
            "id": new_uuid(),
            "type": "treasure",
            "title": encounter["treasure"]["name"],
            "payload": {
                "format": "markdown",
                "content": encounter["treasure"]["description"]
            },
            "sortOrder": sort_order
        })
    
    return artifacts

def build_scene_artifacts(scene, module_id):
    """Build artifacts for a scene (maps, etc.)"""
    artifacts = []
    sort_order = 0
    
    for map_asset in scene.get("maps", []):
        artifacts.append({
            "id": new_uuid(),
            "type": "map",
            "title": map_asset["title"],
            "payload": {
                "url": f"/uploads/{module_id}/{map_asset['filename']}",
                "mimeType": "image/png",
                "caption": map_asset.get("caption", ""),
                "source": "upload"
            },
            "sortOrder": sort_order
        })
        sort_order += 1
    
    return artifacts

def build_encounter(encounter_data, module_id):
    """Build a single encounter from data"""
    encounter = {
        "id": new_uuid(),
        "type": "encounter",
        "title": f"{encounter_data['type'].title()} — {encounter_data['name']}",
        "sortOrder": 0,
        "metadata": {
            "tags": [encounter_data["type"]],
            "level": encounter_data.get("level", 1)
        },
        "narratives": [
            {
                "id": new_uuid(),
                "title": "Main",
                "format": "html",
                "content": encounter_data["description"],
                "sortOrder": 0
            }
        ],
        "artifacts": build_encounter_artifacts(encounter_data, module_id),
        "children": []
    }
    
    return encounter

def build_scene(scene_data, module_id):
    """Build a scene from data"""
    scene = {
        "id": new_uuid(),
        "type": scene_data.get("type", "scene"),
        "title": scene_data["name"],
        "slug": scene_data.get("slug", scene_data["name"].lower().replace(" ", "-")),
        "sortOrder": 0,
        "narratives": [
            {
                "id": new_uuid(),
                "title": "Main",
                "format": "html",
                "content": scene_data["description"],
                "sortOrder": 0
            }
        ],
        "artifacts": build_scene_artifacts(scene_data, module_id),
        "children": []
    }
    
    # Add encounters as children
    for encounter_data in scene_data.get("encounters", []):
        scene["children"].append(build_encounter(encounter_data, module_id))
    
    return scene

def build_act(act_data, module_id):
    """Build an act from data"""
    act = {
        "id": new_uuid(),
        "type": "act",
        "title": f"Act {act_data['number']} — {act_data['title']}",
        "slug": act_data["slug"],
        "sortOrder": act_data["number"] - 1,
        "metadata": {
            "levelRange": act_data["levelRange"]
        },
        "narratives": [
            {
                "id": new_uuid(),
                "title": "Overview",
                "format": "html",
                "content": act_data["overview"],
                "sortOrder": 0
            }
        ],
        "artifacts": [],
        "children": []
    }
    
    # Build scenes
    for scene_data in act_data.get("scenes", []):
        act["children"].append(build_scene(scene_data, module_id))
    
    return act

def build_pcs(data, module_id):
    """Build player characters from data"""
    pcs = []
    
    for pc_data in data.get("pcs", []):
        pc = {
            "id": new_uuid(),
            "name": pc_data["name"],
            "sortOrder": 0,
            "metadata": {
                "ancestry": pc_data.get("ancestry", ""),
                "class": pc_data.get("class", "")
            },
            "narratives": [
                {
                    "id": new_uuid(),
                    "title": "Character Sheet",
                    "format": "html",
                    "content": f"<div class=\"pf2e-sheet\"><h2>{pc_data['name']}</h2><p>Full character sheet would go here.</p></div>",
                    "sortOrder": 0
                },
                {
                    "id": new_uuid(),
                    "title": "Backstory",
                    "format": "markdown",
                    "content": pc_data.get("backstory", ""),
                    "sortOrder": 1
                }
            ],
            "artifacts": [
                {
                    "id": new_uuid(),
                    "type": "handout",
                    "title": "Pathfinder Character Sheet (Remaster)",
                    "payload": {
                        "assetRef": "pf2e-remaster-character-sheet",
                        "url": "https://paizo.com/pathfinder/character-sheet",
                        "mimeType": "application/pdf",
                        "format": "external",
                        "publisher": "Paizo",
                        "rulesSystem": "PF2e"
                    },
                    "sortOrder": 0
                }
            ],
            "sidequests": []
        }
        
        # Add portrait if present
        if "portrait" in pc_data:
            pc["artifacts"].append({
                "id": new_uuid(),
                "type": "image",
                "title": "Portrait",
                "payload": {
                    "url": f"/uploads/{module_id}/{pc_data['portrait']}",
                    "mimeType": "image/png",
                    "role": "portrait"
                },
                "sortOrder": 1
            })
        
        # Add sidequest if present
        if "sidequest" in pc_data:
            pc["sidequests"].append({
                "id": new_uuid(),
                "title": pc_data["sidequest"]["name"],
                "sortOrder": 0,
                "narratives": [
                    {
                        "id": new_uuid(),
                        "title": "Quest Details",
                        "format": "html",
                        "content": pc_data["sidequest"]["description"],
                        "sortOrder": 0
                    }
                ],
                "artifacts": []
            })
        
        pcs.append(pc)
    
    return pcs

def build_npcs(data):
    """Build non-player characters from data"""
    npcs = []
    
    for npc_data in data.get("npcs", []):
        npc = {
            "id": new_uuid(),
            "name": npc_data["name"],
            "sortOrder": 0,
            "metadata": {
                "role": npc_data.get("role", ""),
                "ancestry": npc_data.get("ancestry", "")
            },
            "summary": npc_data.get("summary", ""),
            "narratives": [
                {
                    "id": new_uuid(),
                    "title": "Description",
                    "format": "html",
                    "content": npc_data.get("description", ""),
                    "sortOrder": 0
                }
            ],
            "artifacts": []
        }
        npcs.append(npc)
    
    return npcs

def build_assets(data, module_id):
    """Build assets manifest from all referenced files"""
    assets = []
    seen_urls = set()
    
    # Cover art
    cover = data.get("coverArt", {})
    if cover.get("url"):
        url = f"/uploads/{module_id}/{cover['url']}"
        if url not in seen_urls:
            assets.append({
                "url": url,
                "mimeType": "image/jpeg",
                "role": "cover",
                "referencedBy": ["module.coverArt.0.url"]
            })
            seen_urls.add(url)
    
    # Preview CSS
    assets.append({
        "url": f"/uploads/{module_id}/preview.css",
        "mimeType": "text/css",
        "role": "stylesheet",
        "referencedBy": ["module.previewCssUrl"]
    })
    seen_urls.add(f"/uploads/{module_id}/preview.css")
    
    # PC portraits
    for pc in data.get("pcs", []):
        if "portrait" in pc:
            url = f"/uploads/{module_id}/{pc['portrait']}"
            if url not in seen_urls:
                assets.append({
                    "url": url,
                    "mimeType": "image/png",
                    "role": "image",
                    "referencedBy": [f"module.pc.{pc.get('name', 'unknown')}.artifact.portrait"]
                })
                seen_urls.add(url)
    
    # Maps from acts
    for act in data.get("acts", []):
        for scene in act.get("scenes", []):
            for map_asset in scene.get("maps", []):
                url = f"/uploads/{module_id}/{map_asset['filename']}"
                if url not in seen_urls:
                    assets.append({
                        "url": url,
                        "mimeType": "image/png",
                        "role": "map",
                        "referencedBy": [f"module.act.{act['number']}.scene.{scene['name']}.map"]
                    })
                    seen_urls.add(url)
    
    return assets

def generate_export(data, module_id=None):
    """Generate the complete export JSON"""
    if module_id is None:
        module_id = str(uuid.uuid4())
    
    export = {
        "$schema": "https://adventure-maker-by-act.local/schemas/module-export/v1/module-export.schema.json",
        "formatVersion": "1.0.0",
        "exportedAt": datetime.now().isoformat(),
        "generator": {
            "name": "AdventureMakerByAct Module Generator",
            "version": "1.0.0",
            "url": "https://github.com/edmund-landgraf/AdventureMakerByAct"
        },
        "module": {
            "id": module_id,
            "title": data["title"],
            "subtitle": data["subtitle"],
            "levelRange": data["levelRange"],
            "setting": data["setting"],
            "previewCssUrl": f"/uploads/{module_id}/preview.css",
            "coverArt": [
                {
                    "id": new_uuid(),
                    "url": f"/uploads/{module_id}/{data['coverArt']['url']}",
                    "mimeType": "image/jpeg",
                    "caption": data["coverArt"].get("caption", ""),
                    "sortOrder": 0
                }
            ],
            "createdAt": datetime.now().isoformat(),
            "updatedAt": datetime.now().isoformat(),
            "narratives": build_narratives(data),
            "artifacts": build_artifacts(data, module_id),
            "containers": [],
            "pcs": build_pcs(data, module_id),
            "npcs": build_npcs(data)
        },
        "assets": []
    }
    
    # Build acts
    for act_data in data.get("acts", []):
        export["module"]["containers"].append(build_act(act_data, module_id))
    
    # Build assets manifest
    export["assets"] = build_assets(data, module_id)
    
    return export

def main():
    """Main entry point"""
    print("📖 Loading adventure data...")
    data = load_adventure_data("adventure_data.json")
    
    print(f"📝 Generating module: {data['title']}")
    export = generate_export(data)
    
    output_file = f"{data['title'].lower().replace(' ', '-')}.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(export, f, ensure_ascii=False, indent=2)
    
    print(f"✅ Success! Module exported to: {output_file}")
    print(f"   Module ID: {export['module']['id']}")
    print(f"   Acts: {len(data['acts'])}")
    print(f"   PCs: {len(data['pcs'])}")
    print(f"   NPCs: {len(data['npcs'])}")
    print(f"   Assets: {len(export['assets'])}")
    print("\n📋 Import this file into AdventureMakerByAct")

if __name__ == "__main__":
    main()