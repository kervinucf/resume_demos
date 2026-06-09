#!/usr/bin/env python3
import json, os, shlex, sqlite3;
from pathlib import Path;
from time import perf_counter_ns as ns
from HyperCoreSDK.python.helpers.search import HyperSearch, SearchOptions, Source
from HyperCoreSDK.python.helpers.utils import create_default_storage_directory

D = Path(os.getenv("HYPER_DATA_DIR", create_default_storage_directory())).expanduser();
DB = Path(os.getenv("HYPER_SQLITE_DB", D / "sqlite" / "nodes.sqlite")).expanduser();
URL = os.getenv("HYPER_RELAY_URL", "http://127.0.0.1:8765");
LS = int(os.getenv("MDR_LS_LIMIT", "80"));
TR = int(os.getenv("MDR_TREE_LIMIT", "40"));
SR = int(os.getenv("MDR_SEARCH_LIMIT", "20"));
P = os.getenv("MDR_PROFILE", "1") != "0"
ms = lambda t: (ns() - t) / 1e6;
clean = lambda p="": str(p or "").strip().strip("/").replace("/", ".").strip(".");
d2p = lambda p="": clean(p).replace(".", "/");
slash = lambda p="": ("/" + d2p(p) if d2p(p) else "/");
base = lambda p="": (d2p(p).rsplit("/", 1)[-1] if d2p(p) else "/");
nxt = lambda s: "\U0010ffff" if not s else s[:-1] + chr(ord(s[-1]) + 1)


def dec(r):
    try:
        x = json.loads(r) if r else None
    except Exception:
        return None
    return x.get("data") if isinstance(x, dict) and isinstance(x.get("data"), dict) else x


lab = lambda x: str(x.get("name") or x.get("title") or x.get("display") or "") if isinstance(x, dict) else (
    "" if x is None else str(x));
pretty = lambda x: "" if x is None else (
    json.dumps(x, ensure_ascii=False, indent=2) if isinstance(x, (dict, list)) else str(x));
parts = lambda s: [x.replace("\0", "..") for x in str(s or "").strip().replace("..", "\0").replace("/", ".").split(".")
                   if x and x != "."]


class M:
    def __init__(s):
        s.cwd = "";
        s.k = {};
        s.cx = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        [s.cx.execute("PRAGMA " + p) for p in
         ("query_only=ON", "mmap_size=1073741824", "cache_size=-262144", "temp_store=MEMORY")];
        s.hs = HyperSearch(URL, backend="sql", db_path=str(DB))

    def res(s, a=""):
        a = str(a or "").strip();
        q = [] if a.startswith("/") else (clean(s.cwd).split(".") if s.cwd else [])
        if a in ("", ".", "/"): return "" if a == "/" else s.cwd
        for p in parts(a): q.pop() if p == ".." and q else (q.append(p) if p != ".." else None)
        return ".".join(q)

    def row(s, p):
        p = clean(p);return None if not p else s.cx.execute(
            "select id,path,data from nodes where root=? and path=? limit 1", (p.split(".", 1)[0], d2p(p))).fetchone()

    def kidsq(s, p, l):
        p = clean(p)
        if not p: return [(r, 1, None) for r, _ in s.cx.execute(
            "select root,id from nodes where parent_id is null order by case when root glob '[0-9]*' then 1 else 0 end,root limit ?",
            (l,))], "roots_sql"
        root, pre = p.split(".", 1)[0], d2p(p) + "/";
        hi = nxt(pre);
        cur = pre;
        o = [];
        seen = set()
        while len(o) < l:
            rows = s.cx.execute(
                "select path,data from nodes where root=? and path>=? and path<? order by path limit 256",
                (root, cur, hi)).fetchall()
            if not rows: break
            adv = 0
            for np, raw in rows:
                np = str(np);
                tail = np[len(pre):] if np.startswith(pre) else "";
                h, sep, _ = tail.partition("/")
                if h and h not in seen: seen.add(h);o.append((h, bool(sep) or raw is None, None if sep else dec(raw)))
                cur = pre + nxt(h) if sep and h else np + "\0";
                adv = 1
                if len(o) >= l: break
            if len(rows) < 256: break
            if not adv: cur = str(rows[-1][0]) + "\0"
        return o, "range_sql"

    def children(s, p, l):
        k = (clean(p), l);hit = k in s.k;s.k.setdefault(k, s.kidsq(p, l)[0]);return s.k[
            k], "cache" if hit else "range_sql"

    def isdir(s, p):
        p = clean(p);r = s.row(p);pre = d2p(p) + "/";return not p or bool(r and r[2] is None) or s.cx.execute(
            "select 1 from nodes where root=? and path>=? and path<? limit 1",
            (p.split(".", 1)[0], pre, nxt(pre))).fetchone()

    def ls(s, a):
        t = ns();p = s.res(a[0]) if a else s.cwd;leaf = clean(p) and not s.isdir(p);rows, src = ([],
                                                                                                 None) if leaf else s.children(
            p, LS);print(
            (f"{slash(p)} {lab(dec(s.row(p)[2]))}".rstrip() + f"\n  # view with: view {base(p)}") if leaf else slash(
                p));[print((f"  {n}/ {lab(d)}" if isd else f"  {n} {lab(d)}  # view {n}").rstrip()) for n, isd, d in
                     rows] or (None if leaf or rows else print("  (empty)"));print(
            f"  [profile ls: total={ms(t):.3f}ms {'leaf' if leaf else 'children=' + src}]" if P else "")

    def view(s, a):
        t = ns();p = s.res(a[0]) if a else "";r = s.row(p);print("usage: view <path>" if not a else (
            f"not found: {slash(p)}" if not r else f"is a directory: {slash(p)}" if r[2] is None else pretty(
                dec(r[2]))));print(f"  [profile view: total={ms(t):.3f}ms]" if P and a else "")

    def cd(s, a):
        d = s.res(a[0] if a else "/");ok = s.isdir(d);s.cwd = d if ok else s.cwd;print(
            f"not a directory: {slash(d)}\nuse: view {base(d)}" if not ok else "", end="" if ok else "\n")

    def tree(s, a):
        t = ns();p = s.res(a[0]) if a else s.cwd;print(slash(p));s._tree(p, min(int(a[1]) if len(a) > 1 and a[
            1].isdigit() else 2, 4), "");print(f"  [profile tree: total={ms(t):.3f}ms]" if P else "")

    def _tree(s, p, d, pre):
        rows, _ = s.children(p, TR)
        for i, (n, isd, _) in enumerate(rows): last = i == len(rows) - 1;print(
            pre + ("└─ " if last else "├─ ") + n + ("/" if isd else ""));s._tree(
            (clean(p) + "." if clean(p) else "") + n, d - 1,
            pre + ("   " if last else "│  ")) if isd and d > 1 else None

    def search(s, a):
        t = ns();q = " ".join(a).strip();src = clean(s.cwd);rows = [] if not q else s.hs.query(q, options=SearchOptions(
            exclude=("_meta", "_mdr"), fields=("path", "data"), sources={src: Source(SR, max_depth=8)} if src else {},
            text_mode="index", hybrid_fill_to_count=False, source_workers=1)).to_list();print(
            "usage: search <query>" if not q else "", end="" if not q else "\n");[print(
            f"{i:>3}. {x.get('path')} {lab(x.get('data'))}".rstrip()) for i, x in enumerate(rows, 1)] or (
                                                                                     None if not q else print(
                                                                                         "  (no matches)"));print(
            f"  [profile search: total={ms(t):.3f}ms]" if P and q else "")

    def help(s):
        print(
            "commands: cd <path> · .. · ls [path] · tree [path] [depth] · search <query> · view <path> · cat <path> · quit\ntip: entries without / are values; use view <name> or cat <name> to print them")

    def loop(s):
        print(
            f"mdr-fast: db={DB}\nmdr: cd · .. · ls · tree · search · view · cat · quit\ntip: use view <name> for entries listed without /")
        while 1:
            try:
                raw = input(f"mdr:{slash(s.cwd)}> ").strip()
            except(EOFError, KeyboardInterrupt):
                print();break
            if not raw: continue
            try:
                c, *a = shlex.split(raw)
            except ValueError as e:
                print(f"parse error: {e}");continue
            if c in ("q", "quit", "exit"): break
            {"cd": s.cd, "..": s.cd, "ls": s.ls, "tree": s.tree, "search": s.search, "view": s.view, "cat": s.view,
             "help": lambda a: s.help(), "?": lambda a: s.help()}.get(c, lambda a: s.help())([".."] if c == ".." else a)
        s.cx.close()


if __name__ == "__main__": M().loop()
