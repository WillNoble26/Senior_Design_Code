#!/usr/bin/env python3
# spat_visualizer_final_rate_sync.py
# Final version: controllable --rate, optional --sync-time, smooth countdown, perfect box alignment.

import argparse, time, xml.etree.ElementTree as ET, os, sys, re, unicodedata

# ---------- terminal colors ----------
RESET = "\033[0m"; BOLD  = "\033[1m"
FG = {"red":"\033[31m","yellow":"\033[33m","green":"\033[32m","white":"\033[37m"}
def colorize(txt, c): return FG.get(c,"")+txt+RESET

def clear_screen(enabled=True):
    if enabled:
        os.system("cls" if os.name == "nt" else "clear")

# ---------- width helpers ----------
ANSI = re.compile(r'\x1b\[[0-9;]*m')
def visible_len(s):
    """Return printable width (ignores ANSI codes, handles emoji)."""
    txt = ANSI.sub('', s)
    width = 0
    for ch in txt:
        width += 2 if unicodedata.east_asian_width(ch) in ("W","F") else 1
    return width

# ---------- XML helpers ----------
def first(el, path):
    x = el.find(path)
    return x.text if (x is not None and x.text is not None) else None

def findall(el, path):
    return el.findall(path) or []

def num_in_node_or_kids(node):
    if node is None: return None
    if node.text and node.text.strip().isdigit():
        return int(node.text.strip())
    for sub in node.iter():
        if sub is not node and sub.text and sub.text.strip().isdigit():
            return int(sub.text.strip())
    return None

def num_at_any(el, paths):
    for p in paths:
        n = el.find(p)
        v = num_in_node_or_kids(n)
        if v is not None: return v
    return None

EVENT2COLOR = {
    "protected-Movement-Allowed":"green",
    "permissive-Movement-Allowed":"green",
    "protected-clearance":"yellow",
    "permissive-clearance":"yellow",
    "caution-Conflicting-Traffic":"yellow",
    "stop-And-Remain":"red",
    "stop-Then-Proceed":"red",
    "dark":"red",
}

# =========================================================
#                       MAP PARSER
# =========================================================
def parse_map_blocks(text):
    blocks, start = [], 0
    while True:
        i = text.find("<MapData>", start)
        if i == -1: break
        j = text.find("</MapData>", i)
        if j == -1: break
        blocks.append(text[i:j+len("</MapData>")])
        start = j + len("</MapData>")
    return blocks

def parse_map(md_xml):
    root = ET.fromstring(md_xml)
    ig = root.find(".//intersections/IntersectionGeometry")
    if ig is None: 
        return None, None, {}
    inter_id = first(ig, "id/id") or first(ig, "id")
    inter_name = first(ig, "name") or first(ig, "id/name")
    lanes = {}
    for gl in findall(ig, "laneSet/GenericLane"):
        lid_txt = first(gl, "laneID")
        if not lid_txt: continue
        try: lid = int(lid_txt)
        except ValueError: continue
        sg = None
        for ct in findall(gl, "connectsTo/Connection"):
            sgt = first(ct, "signalGroup")
            if sgt:
                try: sg = int(sgt)
                except ValueError: sg = None
                break
        lanes[lid] = {"sg": sg}
    return inter_id, inter_name, lanes

# =========================================================
#                       SPaT PARSER
# =========================================================
def parse_spat_blocks(text):
    blocks, start = [], 0
    while True:
        i = text.find("<SPAT>", start)
        if i == -1: break
        j = text.find("</SPAT>", i)
        if j == -1: break
        blocks.append(text[i:j+len("</SPAT>")])
        start = j + len("</SPAT>")
    return blocks

def extract_now_value(spat):
    candidates = []
    ts = num_at_any(spat, [".//timeStamp", ".//msecOfMin", ".//dSecond"])
    if ts is not None: candidates.append(ts)
    moy = num_at_any(spat, [".//moy"])
    if moy is not None: candidates.append(moy)
    if not candidates: return None
    for v in candidates:
        if v <= 60000 or v <= 6000 or v <= 600:
            return v
    return candidates[0]

def parse_spat(spat_root):
    spat = spat_root if spat_root.tag == "SPAT" else (spat_root.find(".//SPAT") or spat_root.find(".//value/SPAT"))
    if spat is None: return None, None, []
    inter = spat.find(".//intersections/IntersectionState")
    if inter is None: return None, None, []
    inter_id = first(inter,"id/id") or first(inter,"id")
    now = extract_now_value(spat)

    def read_event_state(ev_el):
        esn = ev_el.find("eventState")
        if esn is None: return None
        kids = list(esn)
        return kids[0].tag if kids else (esn.text.strip() if esn.text else None)

    def read_min_end_any(ev_el):
        for parent in ("timing", "timeChangeDetails"):
            t = ev_el.find(parent)
            if t is None: continue
            for tag in ("likelyTime","minEndTime","maxEndTime","endTime"):
                m = num_at_any(t, [tag])
                if m is not None: return m
        return None

    states=[]
    for ms in findall(inter, "states/MovementState"):
        sg_txt = first(ms, "signalGroup")
        if not sg_txt: continue
        try: sg = int(sg_txt)
        except ValueError: continue
        events = ms.findall("state-time-speed/MovementEvent")
        chosen = None
        for ev_el in events:
            m = read_min_end_any(ev_el)
            if m is not None:
                chosen = (ev_el, m); break
        if chosen is None and events:
            chosen = (events[0], read_min_end_any(events[0]))
        ev_name, met = None, None
        if chosen:
            ev_el, met = chosen
            ev_name = read_event_state(ev_el)
        states.append({"sg": sg, "event": ev_name, "minEndRaw": met})
    return inter_id, now, states

def detect_unit_and_delta(now_val, met_val):
    if now_val is None or met_val is None: return None
    now_sec = (now_val / 1000.0) % 60.0
    end_sec = (met_val / 10.0) % 60.0
    remaining = (end_sec - now_sec) % 60.0
    if 0.0 <= remaining <= 60.0: return remaining
    candidates = []
    for modulo, scale in ((600,10.0),(6000,100.0),(60000,1000.0),(65536,1000.0)):
        rem = (met_val - now_val) % modulo
        candidates.append(rem/scale)
    small = [c for c in candidates if 0 <= c <= 60.0]
    return min(small) if small else min(candidates) % 60.0

# =========================================================
#                       DISPLAY
# =========================================================
def color_emoji(c):
    return {"green":"🟢", "yellow":"🟡", "red":"🔴"}.get(c, "⚪")

def next_color(c):
    return {"green":"yellow", "yellow":"red", "red":"green"}.get(c, None)

def draw_card(approach_name, inter_id, lane_id, sg, cur_color, secs_remaining, clear_enabled=True):
    clear_screen(clear_enabled)
    title = f"Approaching: {approach_name or '—'}  (ID: {inter_id or '—'})"
    print(BOLD + title + RESET)
    print(f"\nOn your lane ↑, the next light ⇒\n")

    cur = cur_color or "red"
    nxt = next_color(cur)
    cur_line = f"{color_emoji(cur)}  CURRENT: {cur.upper():<6}"
    nxt_line = (
        f"{color_emoji(nxt)}  Changes to {nxt.upper():<6} in {secs_remaining:0.1f} s"
        if isinstance(secs_remaining, (int, float))
        else f"{color_emoji(nxt)}  Changes to {nxt.upper():<6} soon"
    )

    vis_cur = visible_len(cur_line)
    vis_nxt = visible_len(nxt_line)
    inner_w = max(vis_cur, vis_nxt)

    def pad_visible(s, width):
        diff = width - visible_len(s)
        return s + " " * max(diff, 0)

    cur_padded = pad_visible(cur_line, inner_w)
    nxt_padded = pad_visible(nxt_line, inner_w)

    top = "┌" + "─" * (inner_w + 4) + "┐"
    mid1 = f"│  {cur_padded}  │"
    mid2 = f"│  {nxt_padded}  │"
    bottom = "└" + "─" * (inner_w + 4) + "┘"

    print(top)
    print(mid1)
    print(mid2)
    print(bottom)
    print(f"\n(lane {lane_id if lane_id is not None else '—'}, SG {sg if sg is not None else '—'})\n")

# =========================================================
#                       MAIN
# =========================================================
def main():
    ap = argparse.ArgumentParser(description="Realtime SPaT visualizer for a selected lane.")
    ap.add_argument("logfile", help="Path to XML log with MapData + SPAT")
    ap.add_argument("--lane", type=int, required=True, help="Your lane ID (from MAP)")
    ap.add_argument("--rate", type=float, default=0.1,
                    help="Delay between SPaT frames (seconds, default 0.1)")
    ap.add_argument("--sync-time", action="store_true",
                    help="Sync playback rate to SPaT timestamp differences (default off)")
    ap.add_argument("--no-clear", action="store_true",
                    help="Disable screen clearing for debug scroll view")
    args = ap.parse_args()

    try:
        text = open(args.logfile, "r", encoding="utf-8", errors="ignore").read()
    except Exception as e:
        print(f"Failed to read file: {e}")
        sys.exit(1)

    map_blocks = parse_map_blocks(text)
    if not map_blocks:
        print("No MapData found in file."); return
    inter_id_map, inter_name_map, lane_info = parse_map(map_blocks[0])
    if args.lane not in lane_info:
        print(f"Lane {args.lane} not found in MAP. Known lanes: {sorted(lane_info.keys())}")
        return
    my_sg = lane_info[args.lane].get("sg")

    spat_blocks = parse_spat_blocks(text)
    if not spat_blocks:
        print("No SPaT found in file."); return

    last_min_end = None
    simulated_rem = None
    last_ts = None
    shown = 0

    for blob in spat_blocks:
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            continue
        inter_id_spat, now_val, states = parse_spat(root)
        if not states: continue

        cur_event, rem_secs = None, None
        for st in states:
            if st["sg"] == my_sg:
                cur_event = st["event"]
                rem_secs = detect_unit_and_delta(now_val, st["minEndRaw"])
                if st["minEndRaw"] == last_min_end and isinstance(simulated_rem, (int,float)):
                    if last_ts is not None and now_val is not None:
                        dt = (now_val - last_ts) / 10.0
                        simulated_rem = max(0.0, simulated_rem - dt)
                    else:
                        simulated_rem = max(0.0, simulated_rem - args.rate)
                else:
                    simulated_rem = rem_secs
                last_min_end = st["minEndRaw"]
                break

        cur_color = EVENT2COLOR.get(cur_event, "red")
        draw_card(
            approach_name=inter_name_map,
            inter_id=(inter_id_map or inter_id_spat),
            lane_id=args.lane,
            sg=my_sg,
            cur_color=cur_color,
            secs_remaining=simulated_rem if isinstance(simulated_rem, (int,float)) else None,
            clear_enabled=not args.no_clear,
        )

        # ---------- Frame timing ----------
        if args.sync_time and last_ts is not None and now_val is not None:
            diff = (now_val - last_ts) / 100.0
            delay = diff if 0.01 <= diff <= 5 else args.rate
        else:
            delay = args.rate

        last_ts = now_val
        time.sleep(delay)
        shown += 1

    if shown == 0:
        print("SPaT frames found, but none referenced your lane's signal group.")

if __name__ == "__main__":
    main()
