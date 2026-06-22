"""
collectors/productivity.py — Advanced Productivity Score Tracker
(See full docstring in uploaded document)
"""

import re
import os
import json
import time
import logging
import threading
import subprocess
from pathlib import Path
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR        = Path.home() / ".local" / "share" / "byteslut"
USER_SITES_FILE = DATA_DIR / "user_sites.json"
TO_REVIEW_FILE  = DATA_DIR / "sites_to_review.json"
ROLE_FILE       = DATA_DIR / "detected_role.json"

SCORE = {
    "coding_editor":      10,
    "terminal":           10,
    "localhost_dev":       9,
    "ai_tool":             8,
    "learning_video":      8,
    "productive_site":     7,
    "writing_docs":        6,
    "dev_tool":            8,
    "work_comm":           5,
    "user_productive":     9,
    "user_neutral":        5,
    "neutral":             5,
    "user_entertainment":  1,
    "entertainment_app":   4,
    "entertainment_fg":    2,
    "social_media":        1,
    "idle":                5,
}

CONFIDENCE = {
    "coding_editor":      1.00,
    "terminal":           0.95,
    "localhost_dev":      1.00,
    "ai_tool":            0.95,
    "learning_video":     0.85,
    "productive_site":    0.90,
    "writing_docs":       0.90,
    "dev_tool":           0.95,
    "work_comm":          0.80,
    "user_productive":    1.00,
    "user_neutral":       1.00,
    "neutral":            0.30,
    "user_entertainment": 1.00,
    "entertainment_app":  0.70,
    "entertainment_fg":   0.80,
    "social_media":       0.90,
    "idle":               0.50,
}

NEUTRAL_SCORE        = SCORE["neutral"]
IDLE_THRESHOLD_SEC   = 300
REVIEW_THRESHOLD_SEC = 120

CODING_EDITORS = {
    "code", "vscodium", "nvim", "vim", "neovim", "emacs", "helix",
    "sublime_text", "sublime-text", "kate", "lapce", "zed", "atom",
    "pycharm", "intellij", "clion", "rider", "goland", "webstorm",
    "fleet", "android-studio", "eclipse", "netbeans", "gedit",
}
TERMINALS = {
    "alacritty", "kitty", "wezterm", "foot", "gnome-terminal",
    "konsole", "xterm", "tilix", "st", "urxvt", "terminator",
    "xfce4-terminal", "hyper", "rxvt",
}
BROWSERS = {
    "firefox", "firefox-bin", "chromium", "google-chrome", "chrome",
    "brave", "brave-browser", "opera", "vivaldi", "librewolf",
    "epiphany", "falkon", "qutebrowser", "nyxt", "midori",
}
WRITING_APPS = {
    "libreoffice", "libreoffice-writer", "libreoffice-calc",
    "libreoffice-impress", "onlyoffice", "obsidian", "logseq",
    "zotero", "okular", "evince", "calibre", "typora", "marktext",
    "apostrophe", "ghostwriter", "gummi", "texstudio", "kile",
}
DEV_TOOLS = {
    "postman", "insomnia", "dbeaver", "datagrip", "tableplus",
    "beekeeper-studio", "gitkraken", "github-desktop", "meld",
    "kdiff3", "wireshark", "ghidra", "burpsuite",
    "virtualbox", "virt-manager", "vmware",
}
DESIGN_TOOLS = {
    "gimp", "inkscape", "blender", "krita", "figma", "darktable",
    "rawtherapee", "kdenlive", "openshot", "pitivi", "audacity",
    "ardour", "lmms",
}
COMM_APPS = {
    "thunderbird", "geary", "evolution", "slack", "element",
    "signal", "telegram-desktop", "teams", "zoom",
}
ENTERTAINMENT_APPS = {
    "spotify", "vlc", "rhythmbox", "clementine", "strawberry",
    "steam", "lutris", "heroic", "pegasus-fe", "retroarch",
}
AI_KEYWORDS = [
    "claude.ai", "chat.openai.com", "chatgpt", "gemini.google.com",
    "copilot", "github copilot", "perplexity.ai", "phind.com",
    "you.com", "openrouter", "venice.ai", "cursor", "codeium",
    "tabnine", "sourcegraph cody", "aider", "continue.dev", "codex",
]
PRODUCTIVE_DOMAINS = [
    "github.com", "gitlab.com", "bitbucket.org", "sourcehut.org",
    "docs.", "devdocs.io", "developer.mozilla.org", "mdn web docs",
    "python.org", "rust-lang.org", "golang.org", "nodejs.org",
    "cppreference", "man7.org", "archlinux.org", "aur.archlinux.org",
    "stackoverflow.com", "stackexchange.com", "stack overflow",
    "superuser.com", "serverfault.com",
    "arxiv.org", "scholar.google", "wikipedia.org",
    "researchgate.net", "pubmed.ncbi", "semanticscholar.org",
    "leetcode.com", "hackerrank.com", "codewars.com", "exercism.io",
    "codeforces.com", "atcoder.jp", "adventofcode.com",
    "pypi.org", "npmjs.com", "crates.io", "pkg.go.dev",
    "kaggle.com", "huggingface.co", "paperswithcode.com",
    "notion.so", "confluence", "jira", "linear.app",
    "news.ycombinator.com", "hacker news", "lobste.rs",
    "coursera.org", "udemy.com", "edx.org", "brilliant.org",
    "freecodecamp.org", "pluralsight.com",
]
ENTERTAINMENT_DOMAINS = [
    "netflix.com", "hotstar.com", "primevideo.com", "hulu.com",
    "disneyplus.com", "hbomax.com", "crunchyroll.com",
    "twitch.tv",
    "twitter.com", "x.com", "instagram.com", "facebook.com",
    "tiktok.com", "snapchat.com", "pinterest.com",
    "9gag.com", "ifunny.co",
]
LEARNING_TITLE_KEYWORDS = [
    "tutorial", "course", "learn", "learning", "lecture", "lesson",
    "how to", "how-to", "explained", "explanation", "introduction to",
    "guide", "masterclass", "workshop", "bootcamp", "crash course",
    "full course", "complete guide", "deep dive",
    "programming", "python", "javascript", "typescript", "rust",
    "golang", "java", "c++", "cpp", "kotlin", "linux",
    "bash", "shell", "terminal", "vim", "neovim", "git", "docker",
    "kubernetes", "devops", "machine learning", "deep learning",
    "neural network", "llm", "artificial intelligence", "data science",
    "algorithm", "data structure", "system design", "database", "sql",
    "networking", "tcp/ip", "rest api", "mathematics", "calculus",
    "linear algebra", "statistics", "physics", "chemistry",
    "freecodecamp", "mit opencourseware", "khan academy",
    "3blue1brown", "fireship", "primeagen", "networkchuck",
    "corey schafer", "sentdex", "andrej karpathy", "yannic kilcher",
    "two minute papers", "lex fridman", "mit", "stanford", "coursera",
]
ENTERTAINMENT_TITLE_KEYWORDS = [
    "music video", "official mv", "official video", "lyrics video",
    "vevo", "lofi", "lo-fi chill", "chill beats",
    "funny", "comedy", "meme", "compilation", "fails",
    "reaction", "reacting to",
    "gameplay", "playthrough", "let's play", "lets play",
    "stream highlights", "best moments",
    "shorts",
]
VIDEO_SITES = [
    "youtube", "youtu.be", "vimeo", "twitch.tv", "odysee",
    "peertube", "rumble", "dailymotion", "bilibili",
]


class UserSiteStore:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._sites  = {}
        self._review = {}
        self._lock   = threading.Lock()
        self._load()

    def _load(self):
        try:
            if USER_SITES_FILE.exists():
                with open(USER_SITES_FILE) as f:
                    self._sites = json.load(f)
        except Exception as e:
            logger.warning(f"Could not load user_sites.json: {e}")
        try:
            if TO_REVIEW_FILE.exists():
                with open(TO_REVIEW_FILE) as f:
                    self._review = json.load(f)
        except Exception:
            pass

    def _save(self):
        try:
            with open(USER_SITES_FILE, "w") as f:
                json.dump(self._sites, f, indent=2)
            with open(TO_REVIEW_FILE, "w") as f:
                json.dump(self._review, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save site data: {e}")

    def get_label(self, domain: str) -> Optional[str]:
        with self._lock:
            if domain in self._sites:
                return self._sites[domain]
            for labeled, label in self._sites.items():
                if domain.endswith(labeled) or labeled.endswith(domain):
                    return label
        return None

    def record_visit(self, domain: str, seconds: float = 5.0):
        if not domain or self.get_label(domain) is not None:
            return
        if UserSiteStore.is_localhost_or_dev(domain):
            return
        with self._lock:
            prev = self._review.get(domain, 0)
            self._review[domain] = prev + seconds
            if prev < REVIEW_THRESHOLD_SEC <= self._review[domain]:
                logger.info(f"New site to review: '{domain}'")
            if self._review[domain] % (seconds * 10) < seconds:
                self._save()

    def add_label(self, domain: str, label: str):
        valid = ("productive", "neutral", "entertainment")
        if label not in valid:
            raise ValueError(f"Label must be one of: {valid}")
        with self._lock:
            self._sites[domain] = label
            self._review.pop(domain, None)
            self._save()

    def get_review_queue(self) -> dict:
        with self._lock:
            return dict(sorted(self._review.items(), key=lambda x: x[1], reverse=True))

    @staticmethod
    def is_localhost_or_dev(domain: str) -> bool:
        if not domain:
            return False
        host = domain.split(":")[0].lower()
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return True
        if re.match(r"^192\.168\.", host) or re.match(r"^10\.", host):
            return True
        local_tlds = (".local", ".dev", ".test", ".localhost", ".internal", ".lan", ".home", ".corp")
        if any(host.endswith(t) for t in local_tlds):
            return True
        dev_prefixes = ("staging.", "preview.", "preprod.", "sandbox.", "dev.", "development.", "test.", "qa.", "uat.")
        if any(p in host for p in dev_prefixes):
            return True
        cloud_previews = (".vercel.app", ".netlify.app", ".pages.dev", ".fly.dev", ".railway.app",
                          ".render.com", ".herokuapp.com", ".azurewebsites.net", ".cloudflare.dev", ".surge.sh")
        if any(host.endswith(p) for p in cloud_previews):
            return True
        return False


class RoleDetector:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.role        = "unknown"
        self._app_counts = {}
        self._lock       = threading.Lock()
        self._load()

    def _load(self):
        try:
            if ROLE_FILE.exists():
                with open(ROLE_FILE) as f:
                    data = json.load(f)
                    self.role        = data.get("role", "unknown")
                    self._app_counts = data.get("app_counts", {})
        except Exception:
            pass

    def _save(self):
        try:
            with open(ROLE_FILE, "w") as f:
                json.dump({"role": self.role, "app_counts": self._app_counts}, f, indent=2)
        except Exception:
            pass

    def record_app(self, app: str):
        if not app:
            return
        with self._lock:
            self._app_counts[app] = self._app_counts.get(app, 0) + 1

    def detect_role(self) -> str:
        with self._lock:
            counts = self._app_counts.copy()
        total = sum(counts.values())
        if total < 100:
            return "unknown"
        dev_count      = sum(counts.get(a, 0) for a in CODING_EDITORS | TERMINALS | DEV_TOOLS)
        designer_count = sum(counts.get(a, 0) for a in DESIGN_TOOLS)
        writer_count   = sum(counts.get(a, 0) for a in WRITING_APPS)
        if dev_count / total > 0.30:     role = "developer"
        elif designer_count / total > 0.20: role = "designer"
        elif writer_count / total > 0.25:   role = "writer"
        else:                               role = "unknown"
        if role != self.role:
            self.role = role
            self._save()
        return role

    def get_neutral_boost(self) -> float:
        return {"developer": 1.3, "designer": 1.2, "writer": 1.1, "researcher": 1.2, "unknown": 1.0}.get(self.role, 1.0)


def extract_domain(title: str) -> Optional[str]:
    if not title:
        return None
    pattern = re.compile(
        r'\b(localhost(?::\d+)?|127\.0\.0\.1(?::\d+)?|(?:[\w-]+\.)+[\w-]{2,})\b',
        re.IGNORECASE
    )
    browser_names = {"firefox", "chromium", "chrome", "brave", "opera", "vivaldi", "librewolf", "epiphany", "falkon"}
    for match in pattern.findall(title):
        if match.lower() not in browser_names:
            return match.lower()
    return None


class ActivityClassifier:
    def __init__(self, user_sites: UserSiteStore, role_detector: RoleDetector):
        self.user_sites    = user_sites
        self.role_detector = role_detector

    def _neutral(self, reason: str, domain: Optional[str] = None):
        boost = self.role_detector.get_neutral_boost()
        score = min(10, round(NEUTRAL_SCORE * boost))
        return ("neutral", score, CONFIDENCE["neutral"], reason, domain)

    def classify(self, title, app):
        title = (title or "").lower()
        app   = (app   or "").lower()
        for kw in AI_KEYWORDS:
            if kw in title or kw in app:
                return ("ai_tool", SCORE["ai_tool"], CONFIDENCE["ai_tool"], f"AI tool: '{kw}'", None)
        if app in CODING_EDITORS:
            return ("coding_editor", SCORE["coding_editor"], CONFIDENCE["coding_editor"], f"Code editor: {app}", None)
        if app in TERMINALS:
            return ("terminal", SCORE["terminal"], CONFIDENCE["terminal"], f"Terminal: {app}", None)
        is_browser = (app in BROWSERS or any(b in app for b in ("firefox","chrome","chromium","brave")))
        if is_browser:
            return self._classify_browser(title, app)
        if app in WRITING_APPS or any(w in app for w in ("libreoffice","office")):
            return ("writing_docs", SCORE["writing_docs"], CONFIDENCE["writing_docs"], f"Writing app: {app}", None)
        if app in DEV_TOOLS:
            return ("dev_tool", SCORE["dev_tool"], CONFIDENCE["dev_tool"], f"Dev tool: {app}", None)
        if app in DESIGN_TOOLS:
            return ("writing_docs", SCORE["writing_docs"], CONFIDENCE["writing_docs"], f"Design tool: {app}", None)
        if app in COMM_APPS:
            return ("work_comm", SCORE["work_comm"], CONFIDENCE["work_comm"], f"Work comm: {app}", None)
        if app in ENTERTAINMENT_APPS:
            return ("entertainment_app", SCORE["entertainment_app"], CONFIDENCE["entertainment_app"], f"Entertainment app: {app}", None)
        return self._neutral(f"Unknown app: '{app or 'none'}'")

    def _classify_browser(self, title, app):
        domain = extract_domain(title)
        if domain:
            label = self.user_sites.get_label(domain)
            if label == "productive":
                return ("user_productive", SCORE["user_productive"], CONFIDENCE["user_productive"], f"You labeled '{domain}' as productive", domain)
            elif label == "entertainment":
                return ("user_entertainment", SCORE["user_entertainment"], CONFIDENCE["user_entertainment"], f"You labeled '{domain}' as entertainment", domain)
            elif label == "neutral":
                return ("user_neutral", SCORE["user_neutral"], CONFIDENCE["user_neutral"], f"You labeled '{domain}' as neutral", domain)
        for kw in AI_KEYWORDS:
            if kw in title:
                return ("ai_tool", SCORE["ai_tool"], CONFIDENCE["ai_tool"], f"AI tool: '{kw}'", domain)
        if domain and UserSiteStore.is_localhost_or_dev(domain):
            return ("localhost_dev", SCORE["localhost_dev"], CONFIDENCE["localhost_dev"], f"Dev server: {domain}", domain)
        for kw in PRODUCTIVE_DOMAINS:
            if kw in title:
                return ("productive_site", SCORE["productive_site"], CONFIDENCE["productive_site"], f"Productive site: '{kw}'", domain)
        for kw in ENTERTAINMENT_DOMAINS:
            if kw in title:
                if any(v in title for v in VIDEO_SITES):
                    return self._classify_video(title, domain)
                if any(s in kw for s in ("twitter","x.com","instagram","facebook","tiktok","snapchat")):
                    return ("social_media", SCORE["social_media"], CONFIDENCE["social_media"], f"Social media: '{kw}'", domain)
                return ("entertainment_fg", SCORE["entertainment_fg"], CONFIDENCE["entertainment_fg"], f"Entertainment: '{kw}'", domain)
        if any(v in title for v in VIDEO_SITES):
            return self._classify_video(title, domain)
        learn  = sum(1 for kw in LEARNING_TITLE_KEYWORDS if kw in title)
        entert = sum(1 for kw in ENTERTAINMENT_TITLE_KEYWORDS if kw in title)
        if learn > 0 and learn > entert:
            matched = next(kw for kw in LEARNING_TITLE_KEYWORDS if kw in title)
            return ("productive_site", SCORE["productive_site"], 0.65, f"Learning keyword: '{matched}'", domain)
        if entert > 0 and entert >= learn:
            matched = next(kw for kw in ENTERTAINMENT_TITLE_KEYWORDS if kw in title)
            return ("entertainment_fg", SCORE["entertainment_fg"], 0.65, f"Entertainment keyword: '{matched}'", domain)
        if domain:
            self.user_sites.record_visit(domain, seconds=5)
        return self._neutral(f"Unknown site: {domain or 'no domain'}", domain)

    def _classify_video(self, title, domain):
        learn  = sum(1 for kw in LEARNING_TITLE_KEYWORDS if kw in title)
        entert = sum(1 for kw in ENTERTAINMENT_TITLE_KEYWORDS if kw in title)
        if learn > 0 and learn > entert:
            matched = next(kw for kw in LEARNING_TITLE_KEYWORDS if kw in title)
            return ("learning_video", SCORE["learning_video"], CONFIDENCE["learning_video"], f"Learning video: '{matched}'", domain)
        if entert > 0 and entert >= learn:
            matched = next(kw for kw in ENTERTAINMENT_TITLE_KEYWORDS if kw in title)
            return ("entertainment_fg", SCORE["entertainment_fg"], CONFIDENCE["entertainment_fg"], f"Entertainment video: '{matched}'", domain)
        return self._neutral("Video: ambiguous", domain)


class WindowDetector:
    def __init__(self):
        self.method = self._detect_method()
        logger.info(f"WindowDetector: using '{self.method}'")

    def _detect_method(self):
        for cmd, name in [
            (["swaymsg", "-t", "get_tree"],           "swaymsg"),
            (["hyprctl", "activewindow", "-j"],        "hyprctl"),
            (["xdotool", "getactivewindow"],           "xdotool"),
            (["xprop", "-root", "_NET_ACTIVE_WINDOW"], "xprop"),
        ]:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                if r.returncode == 0:
                    return name
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        return "proc"

    def get_active_window(self):
        try:
            if self.method == "swaymsg":  return self._sway()
            if self.method == "hyprctl":  return self._hyprland()
            if self.method == "xdotool":  return self._xdotool()
            if self.method == "xprop":    return self._xprop()
            return self._proc_fallback()
        except Exception as e:
            logger.debug(f"Window detection error: {e}")
            return None, None

    def _sway(self):
        r = subprocess.run(["swaymsg","-t","get_tree"], capture_output=True, text=True, timeout=3)
        if r.returncode != 0: return None, None
        def find(node):
            if node.get("focused"):
                return (node.get("name") or "").lower() or None, (node.get("app_id") or node.get("window_properties",{}).get("class") or "").lower() or None
            for child in node.get("nodes",[]) + node.get("floating_nodes",[]):
                res = find(child)
                if res[0] or res[1]: return res
            return None, None
        return find(json.loads(r.stdout))

    def _hyprland(self):
        r = subprocess.run(["hyprctl","activewindow","-j"], capture_output=True, text=True, timeout=3)
        if r.returncode != 0: return None, None
        d = json.loads(r.stdout)
        return (d.get("title") or "").lower() or None, (d.get("class") or "").lower() or None

    def _xdotool(self):
        r = subprocess.run(["xdotool","getactivewindow","getwindowname"], capture_output=True, text=True, timeout=3)
        title = r.stdout.strip().lower() if r.returncode == 0 else None
        pr = subprocess.run(["xdotool","getactivewindow","getwindowpid"], capture_output=True, text=True, timeout=3)
        app = None
        if pr.returncode == 0:
            try:
                with open(f"/proc/{pr.stdout.strip()}/comm") as f: app = f.read().strip().lower()
            except: pass
        return title, app

    def _xprop(self):
        r = subprocess.run(["xprop","-root","_NET_ACTIVE_WINDOW"], capture_output=True, text=True, timeout=3)
        if r.returncode != 0: return None, None
        m = re.search(r"0x[0-9a-f]+", r.stdout)
        if not m: return None, None
        pr = subprocess.run(["xprop","-id",m.group(0),"WM_NAME","WM_CLASS"], capture_output=True, text=True, timeout=3)
        title = app = None
        for line in pr.stdout.splitlines():
            if "WM_NAME" in line:
                found = re.search(r'"(.+)"', line)
                if found: title = found.group(1).lower()
            elif "WM_CLASS" in line:
                found = re.search(r'"(\w+)"', line)
                if found: app = found.group(1).lower()
        return title, app

    def _proc_fallback(self):
        try:
            import psutil
            for proc in psutil.process_iter(["name"]):
                name = (proc.info.get("name") or "").lower()
                if name in CODING_EDITORS | BROWSERS | TERMINALS | WRITING_APPS:
                    return None, name
        except: pass
        return None, None


class ProductivityCollector:
    def __init__(self, batch_writer, sample_interval=5, flush_interval=60):
        self.batch_writer    = batch_writer
        self.sample_interval = sample_interval
        self.flush_interval  = flush_interval
        self.running         = False
        self.user_sites      = UserSiteStore()
        self.role_detector   = RoleDetector()
        self.detector        = WindowDetector()
        self.classifier      = ActivityClassifier(self.user_sites, self.role_detector)
        self._samples        = []
        self._lock           = threading.Lock()
        self.last_input_time = time.time()

    def record_input_event(self):
        self.last_input_time = time.time()

    def _is_idle(self) -> bool:
        return (time.time() - self.last_input_time) > IDLE_THRESHOLD_SEC

    def _sample(self):
        if self._is_idle():
            with self._lock:
                self._samples.append({"category":"idle","score":SCORE["idle"],"confidence":CONFIDENCE["idle"],"reason":f"Idle","domain":None,"app":"","title":"","timestamp":time.time()})
            return
        title, app = self.detector.get_active_window()
        if app: self.role_detector.record_app(app)
        category, score, confidence, reason, domain = self.classifier.classify(title, app)
        with self._lock:
            self._samples.append({"category":category,"score":score,"confidence":confidence,"reason":reason,"domain":domain,"app":app or "","title":(title or "")[:100],"timestamp":time.time()})
        logger.debug(f"[{category}] score={score} conf={confidence:.2f} — {reason}")

    def _flush(self):
        with self._lock:
            samples       = self._samples[:]
            self._samples = []
        if not samples: return
        total_weight = sum(s["confidence"] for s in samples)
        if total_weight == 0: return
        weighted_avg = sum(s["score"] * s["confidence"] for s in samples) / total_weight
        s_min, s_max = min(SCORE.values()), max(SCORE.values())
        norm = max(0.0, min(100.0, round(((weighted_avg - s_min) / (s_max - s_min)) * 100, 1)))
        cat_counts    = {}
        domain_counts = {}
        for s in samples:
            cat_counts[s["category"]] = cat_counts.get(s["category"], 0) + 1
            if s["domain"]: domain_counts[s["domain"]] = domain_counts.get(s["domain"], 0) + 1
        dominant = max(cat_counts, key=cat_counts.get)
        role     = self.role_detector.detect_role()
        top_doms = sorted(domain_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        now, today = int(time.time()), str(date.today())
        self.batch_writer.add("productivity", {
            "timestamp":             now,
            "date":                  today,
            "productivity_score":    norm,
            "raw_score_weighted":    round(weighted_avg, 3),
            "dominant_category":     dominant,
            "detected_role":         role,
            "sample_count":          len(samples),
            "total_confidence":      round(total_weight, 2),
            "category_breakdown":    json.dumps(cat_counts),
            "top_domains":           json.dumps(dict(top_doms)),
            "coding_samples":        (cat_counts.get("coding_editor",0)+cat_counts.get("terminal",0)+cat_counts.get("localhost_dev",0)+cat_counts.get("dev_tool",0)),
            "learning_samples":      (cat_counts.get("learning_video",0)+cat_counts.get("productive_site",0)),
            "ai_samples":            cat_counts.get("ai_tool",0),
            "neutral_samples":       (cat_counts.get("neutral",0)+cat_counts.get("user_neutral",0)+cat_counts.get("idle",0)),
            "entertainment_samples": (cat_counts.get("entertainment_fg",0)+cat_counts.get("entertainment_app",0)+cat_counts.get("social_media",0)),
        })
        logger.info(f"Productivity flush: {norm}/100 | role={role} | dominant={dominant} | {len(samples)} samples")

    def run(self):
        self.running = True
        last_flush   = time.time()
        logger.info(f"ProductivityCollector started (sample={self.sample_interval}s flush={self.flush_interval}s)")
        while self.running:
            try:
                self._sample()
            except Exception as e:
                logger.error(f"Sample error: {e}")
            now = time.time()
            if (now - last_flush) >= self.flush_interval:
                try:
                    self._flush()
                except Exception as e:
                    logger.error(f"Flush error: {e}")
                last_flush = now
            time.sleep(self.sample_interval)

    def stop(self):
        self.running = False
        self._flush()

    def label_site(self, domain: str, label: str):
        self.user_sites.add_label(domain, label)

    def get_sites_to_review(self) -> dict:
        return self.user_sites.get_review_queue()
