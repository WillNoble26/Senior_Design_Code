#!/usr/bin/env python3  # run with Python 3

# spat_show_all_frames.py  # filename (just informational)
# Shows a card for YOUR lane: current light, next light, and time-to-change.  # overview

import argparse, time, xml.etree.ElementTree as ET, os, sys  # stdlib imports used below

# ---------- terminal colors ----------
RESET = "\033[0m"                      # ANSI code: reset styles
BOLD  = "\033[1m"                      # ANSI code: bold text
FG = {                                 # ANSI foreground color codes
    "red":"\033[31m",
    "yellow":"\033[33m",
    "green":"\033[32m",
    "white":"\033[37m",
}
def colorize(txt, c):                  # colorize a string with color c
    return FG.get(c, "") + txt + RESET
def clear_screen():                    # clear terminal screen cross-platform
    os.system("cls" if os.name == "nt" else "clear")

# ---------- small XML helpers ----------
def first(el, path):                   # get text of the first of something in a path, or None
    x = el.find(path)                  # find first element
    return x.text if (x is not None and x.text is not None) else None  # safe return

def findall(el, path):                 # get every instance of something in a path (empty list if none)
    return el.findall(path) or []

def num_in_node_or_kids(node):         # find first integer text in node or descendants
    if node is None:                   # if no node found, no number is returned
        return None
    if node.text and node.text.strip().isdigit():  # is the node text a digit?
        return int(node.text.strip())  # convert node text to an int value and return it
    for sub in node.iter():            # check descendants
        if sub is not node and sub.text and sub.text.strip().isdigit():  # is the subnode text a digit?
            return int(sub.text.strip())  # convert node text to an int value and return it
    return None                        # nothing found

def num_at_any(el, paths):             # try many paths and return first numeric value
    for p in paths:                    # going through every possible path
        n = el.find(p)                 # find node at path
        v = num_in_node_or_kids(n)     # pull first integer from that node or its kids
        if v is not None:              # if found
            return v                   # return it
    return None                        # return None if none of the paths had integers

# Map J2735 event-state names to simple colors used by the UI
EVENT2COLOR = {
    "protected-Movement-Allowed":"green",     # protected green
    "permissive-Movement-Allowed":"green",    # permissive green
    "protected-clearance":"yellow",            # yellow phase
    "permissive-clearance":"yellow",          # yellow phase
    "caution-Conflicting-Traffic":"yellow",   # caution yellow
    "stop-And-Remain":"red",                  # red
    "stop-Then-Proceed":"red",                # red (flashing/stop-then-go)
    "dark":"red",                             # treat dark as red for safety
}

DEBUG_TIMING = False                  # print raw timing for debugging when True

# =========================================================
#                       MAP
# =========================================================
def parse_map_blocks(text):           # extract all <MapData>...</MapData> chunks
    blocks, start = [], 0             # list to fill + scanning index
    while True:                       # loop until no more blocks
        i = text.find("<MapData>", start)     # find next start tag
        if i == -1: break             # if none found, break
        j = text.find("</MapData>", i)        # find matching end tag
        if j == -1: break             # if none found, break
        blocks.append(text[i:j+len("</MapData>")])  # slice the block
        start = j + len("</MapData>") # move cursor past this block
    return blocks                     # return list of MAP XML strings

def parse_map(md_xml):                # parse ONE MapData block
    """
    Returns (inter_id, inter_name, lanes)
      lanes: { lane_id(int) : {"sg": signalGroup (int or None)} }
    """
    root = ET.fromstring(md_xml)      # parse XML into a tree
    ig = root.find(".//intersections/IntersectionGeometry")  # main MAP body
    if ig is None:                    # missing MAP content?
        return None, None, {}         # return empty info
    inter_id   = first(ig, "id/id") or first(ig, "id")    # intersection ID (varies by vendor)
    inter_name = first(ig, "name")   or first(ig, "id/name")  # optional vendor-supplied name

    lanes = {}                        # create dict for lanes found to do signal-group (SG) mapping
    for gl in findall(ig, "laneSet/GenericLane"):  # loop all lanes
        lid_txt = first(gl, "laneID") # lane ID text
        if not lid_txt:               # if missing laneID, skip
            continue
        try:
            lid = int(lid_txt)        # convert to int
        except ValueError:
            continue                  # if non-numeric, skip

        sg = None                     # start off assuming no SG
        for ct in findall(gl, "connectsTo/Connection"):  # check lane connections
            sgt = first(ct, "signalGroup")  # read SG text
            if sgt:                   # if present
                try:
                    sg = int(sgt)     # convert to int
                except ValueError:
                    sg = None         # malformed SG value
                break                 # take the first SG found
        lanes[lid] = {"sg": sg}       # record mapping
    return inter_id, inter_name, lanes # send MAP results back

# =========================================================
#                       SPaT
# =========================================================
def parse_spat_blocks(text):          # extract all <SPAT>...</SPAT> chunks
    blocks, start = [], 0             # list + scan index
    while True:                       # loop through file
        i = text.find("<SPAT>", start)        # next SPaT start
        if i == -1: break             # if none found, break
        j = text.find("</SPAT>", i)           # end tag
        if j == -1: break             # if none found, break
        blocks.append(text[i:j+len("</SPAT>")])  # slice block
        start = j + len("</SPAT>")    # advance cursor
    return blocks                     # return list of SPaT XML strings

def extract_now_value(spat):          # find device's current time counter (inside SPaT)
    """
    Try common counters (msecOfMin, dSecond, moy). Prefer per-minute values.
    """
    candidates = []                   # possible counters found
    ts = num_at_any(spat, [           # look for timestamp fields
        ".//timeStamp", ".//timeStamp/msecOfMin", ".//msecOfMin",
        ".//timeStamp/dSecond", ".//dSecond"
    ])
    if ts is not None:
        candidates.append(ts)         # add candidate if found
    moy = num_at_any(spat, [".//moy"])  # minute-of-year (some devices)
    if moy is not None:
        candidates.append(moy)        # add candidate if found
    if not candidates:
        return None                   # nothing usable

    for v in candidates:              # prefer within-minute counters
        if v <= 60000 or v <= 6000 or v <= 600:  # ms/cs/ds ranges
            return v                  # return that value
    return candidates[0]              # else return first candidate

def parse_spat(spat_root):            # parse ONE SPaT block
    # find SPaT node even if nested under other wrappers
    spat = spat_root if spat_root.tag == "SPAT" else (spat_root.find(".//SPAT") or spat_root.find(".//value/SPAT"))
    if spat is None:
        return None, None, []         # if no SPaT found, return None

    inter = spat.find(".//intersections/IntersectionState")  # main per-SG states
    if inter is None:
        return None, None, []         # if no IntersectionState found, return None

    inter_id = first(inter, "id/id") or first(inter, "id")  # intersection ID (from SPaT side)
    now = extract_now_value(spat)     # get current time value from the SPaT element

    def read_event_state(ev_el):      # get textual eventState from a MovementEvent (return None if none found)
        esn = ev_el.find("eventState")
        if esn is None:
            return None
        kids = list(esn)              # some encoders nest as a single child tag
        return kids[0].tag if kids else (esn.text.strip() if esn.text else None)

    def read_min_end_any(ev_el):      # get end-time counter from likely spots
        for parent in ("timing", "timeChangeDetails"):  # first, check these common containers
            t = ev_el.find(parent)
            if t is None:
                continue
            for tag in ("likelyTime", "minEndTime", "maxEndTime", "endTime"):  # common end-time fields
                m = num_at_any(t, [tag])   # read number if present
                if m is not None:
                    return m               # return first usable one
        return None                        # none found

    states = []                            # list of SG state dictionaries to return
    for ms in findall(inter, "states/MovementState"):  # loop through each MovementState (one SG)
        sg_txt = first(ms, "signalGroup")  # find SG number as text
        if not sg_txt:
            continue
        try:
            sg = int(sg_txt)               # convert SG text number to an int
        except ValueError:
            continue

        events = ms.findall("state-time-speed/MovementEvent")  # find all possible events
        ev_name, met = None, None          # event name + raw end-time counter
        chosen = None                      # best MovementEvent to use

        for ev_el in events:               # prefer an event that has timing
            m = read_min_end_any(ev_el)
            if m is not None:
                chosen = (ev_el, m)        # pick this one
                break
        if chosen is None and events:      # else just take first for the name
            chosen = (events[0], read_min_end_any(events[0]))

        if chosen:                         # if we picked something
            ev_el, met = chosen
            ev_name = read_event_state(ev_el)

        if DEBUG_TIMING:                   # optional debug print
            print(f"[dbg] sg={sg} state={ev_name} now={now} minEndRaw={met}")

        states.append({"sg": sg, "event": ev_name, "minEndRaw": met})  # record this SG

    return inter_id, now, states           # return SPaT results

def detect_unit_and_delta(now_val, met_val):  # convert counters to seconds remaining
    """
    Primary guess: now ~ ms-of-minute, end ~ deciseconds-of-minute.
    Fallbacks: try other common wraps/scales (ds, cs, ms, 16-bit wrap).
    """
    if now_val is None or met_val is None:    # if missing data, computation not possible
        return None

    # Primary interpretation (common vendor pairing).
    now_sec = (now_val / 1000.0) % 60.0       # ms to seconds within minute
    end_sec = (met_val / 10.0)   % 60.0       # ds to seconds within minute
    remaining = (end_sec - now_sec) % 60.0    # positive delta wrapped to 0..60
    if 0.0 <= remaining <= 60.0:
        return remaining                      # return the value that's between 0 and 60

    # Alternate interpretations to handle vendor differences (unit + wrap combos).
    candidates = []
    for modulo, scale in ((600, 10.0), (6000, 100.0), (60000, 1000.0), (65536, 1000.0)):
        rem = (met_val - now_val) % modulo     # wrap difference
        candidates.append(rem / scale)         # convert to seconds

    small = [c for c in candidates if 0 <= c <= 60.0]  # reasonable results
    if small:
        return min(small)                      # choose the smallest positive if available
    best = min(candidates)                     # fallback: lowest of all candidates
    return best % 60.0                         # keep within 0..60

# ---------- UI helpers ----------
def color_emoji(c):                            # map color string to emoji dot
    return {"green":"🟢", "yellow":"🟡", "red":"🔴"}.get(c, "⚪")

def next_color(c):                              # simple light sequence
    if c == "green":  return "yellow"
    if c == "yellow": return "red"
    if c == "red":    return "green"
    return None

def draw_card(approach_name, inter_id, lane_id, sg, cur_color, secs_remaining):  # print the card
    clear_screen()                            # wipe screen for fresh frame
    title = f"Approaching: {approach_name or '—'}  (ID: {inter_id or '—'})"  # header line
    print(BOLD + title + RESET)               # bold title
    print()                                   # blank line

    header = "On your lane ↑, the next light ⇒"  # subheader
    print(header)                              # print subheader
    print()                                    # blank line

    cur = cur_color or "red"                   # default to red if unknown
    nxt = next_color(cur)                      # guess next color
    cur_line = f"{color_emoji(cur)}  CURRENT: {cur.upper():<6}"  # current line text
    if isinstance(secs_remaining, (int, float)):                   # if we have timing
        nxt_line = f"{color_emoji(nxt)}  Changes to {nxt.upper():<6} in {secs_remaining:0.1f} s"
    else:                                      # otherwise say "soon"
        nxt_line = f"{color_emoji(nxt)}  Changes to {nxt.upper():<6} soon"

    w = max(len(cur_line), len(nxt_line)) + 4  # box width based on content
    print("┌" + "─"*w + "┐")                    # top border
    print("│ " + cur_line.ljust(w) + " │")      # current line row
    print("│ " + nxt_line.ljust(w) + " │")      # next line row
    print("└" + "─"*w + "┘")                    # bottom border
    print()                                     # blank line
    print(f"(lane {lane_id if lane_id is not None else '—'}, SG {sg if sg is not None else '—'})")  # print lane ID and SG meta
    print()                                     # blank line

# ---------- main ----------
def main():                                     # program entry point
    ap = argparse.ArgumentParser(description="Show your-lane light + next change from J2735 SPaT logs.")  # command line interface (CLI)
    ap.add_argument("logfile", help="Path to XML log with MapData + SPaT")   # argument for reading in logfile
    ap.add_argument("--lane", type=int, required=True, help="Your lane ID (from MAP)")  # required lane ID input from user
    ap.add_argument("--rate", type=float, default=0.5, help="Pause between frames (seconds, default 0.5)")  # optional refresh rate
    args = ap.parse_args()                         # parse CLI args

    try:
        text = open(args.logfile, "r", encoding="utf-8", errors="ignore").read()  # read whole file
    except Exception as e:                        # file error?
        print(f"Failed to read file: {e}")        # show reason
        sys.exit(1)                               # hard exit

    map_blocks = parse_map_blocks(text)           # slice out all MAP blocks
    if not map_blocks:                            # none found?
        print("No MapData found in file."); return
    inter_id_map, inter_name_map, lane_info = parse_map(map_blocks[0])  # parse first MAP

    if args.lane not in lane_info:                # ensure lane exists
        print(f"Lane {args.lane} not found in MAP. Known lanes: {sorted(lane_info.keys())}")
        return
    my_sg = lane_info[args.lane].get("sg")        # lane's SG

    spat_blocks = parse_spat_blocks(text)         # slice out all SPaT blocks
    if not spat_blocks:                           # none found?
        print("No SPaT found in file."); return

    shown = 0                                     # used to count the frames shown
    for blob in spat_blocks:                      # iterate each SPaT frame
        try:
            root = ET.fromstring(blob)            # parse SPaT XML
        except ET.ParseError:
            continue                              # skip if bad frame
        inter_id_spat, now_val, states = parse_spat(root)  # decode SPaT content
        if not states:                            # skip if empty frame
            continue

        cur_event, rem_secs = None, None          # defaults for this frame
        for st in states:                         # search for our SG
            if st["sg"] == my_sg:                 # found our SG
                cur_event = st["event"]           # current event name
                rem_secs  = detect_unit_and_delta(now_val, st["minEndRaw"])  # seconds until change
                break                              # stop searching

        cur_color = EVENT2COLOR.get(cur_event, "red")  # map event to color (default red)
        draw_card(                                   # draw out the card for this frame
            approach_name=inter_name_map,            # intersection name (if available)
            inter_id=(inter_id_map or inter_id_spat),# prefer interID from MAP, else use SPaT's
            lane_id=args.lane,                       # your lane
            sg=my_sg,                                # your signal group
            cur_color=cur_color,                     # current color
            secs_remaining=rem_secs if isinstance(rem_secs, (int, float)) else None,  # seconds (or None)
        )
        shown += 1                                   # go to the next frame
        time.sleep(max(0.05, args.rate))            # pause at rate or 0.05 seconds (whichever is greater)

    if shown == 0:                                   # print the following if no SG found for the user's lane
        print("SPaT frames found, but none referenced your lane's signal group.")

if __name__ == "__main__":                           # only run main when executed directly
    main()                                           # start program
