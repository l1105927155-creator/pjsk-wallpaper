# -*- coding: utf-8 -*-
"""gallery_update.py — 图库更新脚本（独立于壁纸轮换，仅此脚本联网）

用法:
  pythonw gallery_update.py            # 按 config.json 的 update 设置拉新图
  python gallery_update.py             # 同上（控制台可见日志）
由 GUI 手动触发，或按 GUI 设置的间隔自动触发；FIFO 驱逐旧图保持上限。
日志写入 logs/update.log。
"""
import io
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image

import core

MASTER_BASE = "https://sekai-world.github.io"
I18N_BASE = "https://i18n-json.sekai.best/zh-CN"
ASSET_BASE = "https://storage.sekai.best"

REGIONS = {
    "jp": {"db": f"{MASTER_BASE}/sekai-master-db-diff", "assets": f"{ASSET_BASE}/sekai-jp-assets"},
    "cn": {"db": f"{MASTER_BASE}/sekai-master-db-cn-diff", "assets": f"{ASSET_BASE}/sekai-cn-assets"},
    "en": {"db": f"{MASTER_BASE}/sekai-master-db-en-diff", "assets": f"{ASSET_BASE}/sekai-en-assets"},
    "kr": {"db": f"{MASTER_BASE}/sekai-master-db-kr-diff", "assets": f"{ASSET_BASE}/sekai-kr-assets"},
    "tw": {"db": f"{MASTER_BASE}/sekai-master-db-tc-diff", "assets": f"{ASSET_BASE}/sekai-tc-assets"},
}

UNIT_NAMES = {
    "light_sound": ["leo/need", "l/n", "ln", "leo", "乐队"],
    "idol": ["more more jump!", "mmj", "moremore"],
    "street": ["vivid bad squad", "vbs", "bad squad"],
    "theme_park": ["wonderlands×showtime", "wonderlands*showtime", "wxs", "wonderlands", "showtime"],
    "school_refusal": ["25时，在nightcord。", "25时", "25ji", "niigo", "nightcord"],
    "piapro": ["vocaloid", "虚拟歌手", "piapro", "virtual singer"],
}

CHARA_ALIASES = {
    "ichika": 1, "saki": 2, "honami": 3, "shiho": 4, "minori": 5, "haruka": 6,
    "airi": 7, "shizuku": 8, "an": 10, "akito": 11, "toya": 12, "tsukasa": 13,
    "emu": 14, "nene": 15, "rui": 16, "kanade": 17, "mafuyu": 18, "ena": 19,
    "mizuki": 20, "miku": 21, "rin": 22, "len": 23, "luka": 24, "meiko": 25, "kaito": 26,
}

RARITY = {"rarity_1": 1, "rarity_2": 2, "rarity_3": 3, "rarity_4": 4, "rarity_birthday": 0}
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) pjsk-wallpaper/1.0"}
CACHE_TTL = 24 * 3600

LOG = core.APP_DIR / "logs" / "update.log"


def log(msg: str):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fetch_json(url: str, fresh: bool = False):
    core.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("//", 1)[1])
    cache = core.CACHE_DIR / name
    if not fresh and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL:
        return json.loads(cache.read_text("utf-8"))
    log(f"拉取 {url}")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            cache.write_bytes(data)
            return json.loads(data.decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code in (403, 404):
                raise
            log(f"  重试 {attempt + 1}/3（{type(e).__name__}）")
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"下载失败: {url}")


def find_unit(text: str):
    t = text.strip().lower()
    for unit, aliases in UNIT_NAMES.items():
        if t == unit or t in aliases or any(t in a for a in aliases):
            return unit
    return None


def find_chara(text: str, chara_names: dict):
    t = text.strip().lower()
    if t.isdigit():
        return int(t)
    if t in CHARA_ALIASES:
        return CHARA_ALIASES[t]
    for cid, names in chara_names.items():
        full = f"{names.get('firstName', '')}{names.get('givenName', '')}"
        if t in full.lower() or t in names.get("givenName", "").lower():
            return cid
    return None


def chara_unit_map(charas: list) -> dict:
    return {c["id"]: c.get("unit", "") for c in charas}


def pick_candidates(cards, up, chara_names, chara_unit):
    pool = cards
    if up.get("chara"):
        cid = find_chara(up["chara"], chara_names)
        if cid is None:
            raise RuntimeError(f"找不到角色: {up['chara']}")
        pool = [c for c in pool if c["characterId"] == cid]
    if up.get("unit"):
        unit = find_unit(up["unit"])
        if unit is None:
            raise RuntimeError(f"找不到组合: {up['unit']}")
        units = {unit, "piapro"} if unit == "piapro" else {unit}
        pool = [c for c in pool if chara_unit.get(c["characterId"]) in units]
    if up.get("min_rarity"):
        pool = [c for c in pool if RARITY.get(c.get("cardRarityType"), 0) >= up["min_rarity"]]
    if up.get("trained"):
        pool = [c for c in pool if c.get("cardRarityType") in ("rarity_3", "rarity_4")]
    return pool


def download_card_image(card, region_urls, trained):
    abn = card["assetbundleName"]
    variants = ["card_after_training", "card_normal"] if trained else ["card_normal", "card_after_training"]
    for variant in variants:
        for ext in ("webp", "png"):
            url = f"{region_urls['assets']}/character/member/{abn}/{variant}.{ext}"
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.read(), url
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    continue
                raise
    raise RuntimeError(f"卡片 {card['id']} 没有可用卡面图")


def existing_ids() -> set:
    """图库中已有卡图 id（card_<id>_trained.png / card_<id>.png）。"""
    ids = set()
    if core.PJSK_DIR.is_dir():
        for p in core.PJSK_DIR.glob("card_*.png"):
            m = re.match(r"card_(\d+)", p.name)
            if m:
                ids.add(int(m.group(1)))
    return ids


def run_update(cfg: dict) -> list:
    up = cfg.get("update", {})
    count = max(1, int(up.get("count", 3)))
    cap = max(1, int(cfg.get("cap_pjsk", 20)))
    region = up.get("region", "jp")
    urls = REGIONS[region]
    screen = cfg.get("screen") or core.detect_screen()

    cards = fetch_json(f"{urls['db']}/cards.json")
    charas = fetch_json(f"{urls['db']}/gameCharacters.json")
    chara_names = fetch_json(f"{I18N_BASE}/character_name.json")
    prefix_i18n = fetch_json(f"{I18N_BASE}/card_prefix.json")
    chara_unit = chara_unit_map(charas)

    pool = pick_candidates(cards, up, chara_names, chara_unit)
    if not pool:
        raise RuntimeError("筛选条件下没有卡片")
    have = existing_ids()
    pool = [c for c in pool if c["id"] not in have]
    rng = random.Random()
    picked = []
    pool_sorted = sorted(pool, key=lambda c: -c["id"])[: max(count * 4, count)]
    picked = rng.sample(pool_sorted, min(count, len(pool_sorted)))

    added = []
    for card in picked:
        trained = bool(up.get("trained"))
        img_bytes, img_url = download_card_image(card, urls, trained)
        name = f"card_{card['id']}{'_trained' if trained else ''}.png"
        img = Image.open(io.BytesIO(img_bytes))
        core.process_fill(img, core.PJSK_DIR / name, screen)
        names = chara_names.get(str(card["characterId"]), {})
        chara = f"{names.get('firstName', '')}{names.get('givenName', '')}".strip()
        prefix = prefix_i18n.get(str(card["id"]), "")
        added.append((card["id"], chara, prefix))
        log(f"+ {name}  {chara} «{prefix}»")

    enforce_cap(cap)
    return added


def enforce_cap(cap: int):
    """FIFO：按文件修改时间驱逐最旧的 pjsk 图，保持数量 ≤ cap。"""
    if not core.PJSK_DIR.is_dir():
        return
    files = sorted(core.PJSK_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    excess = len(files) - cap
    for p in files[:max(0, excess)]:
        try:
            p.unlink()
            log(f"- 驱逐 {p.name}")
        except OSError as e:
            log(f"! 驱逐失败 {p.name}: {e}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = core.config_load()
    try:
        added = run_update(cfg)
        log(f"更新完成：新增 {len(added)} 张，图库现有 {len(list(core.PJSK_DIR.glob('*.png'))) if core.PJSK_DIR.is_dir() else 0} 张 (pjsk)")
    except Exception as e:
        log(f"更新失败: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
