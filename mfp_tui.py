#!/usr/bin/env python3
"""Terminal UI player for musicforprogramming.net."""

from __future__ import annotations

import curses
import os
import random
import shutil
import signal
import subprocess
import tempfile
import textwrap
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List, Optional

RSS_CANDIDATES = [
    "https://musicforprogramming.net/rss.xml",
    "https://musicforprogramming.net/rss.php",
]
HTTP_TIMEOUT = 20
UA = "musicforprogramming-tui/1.0"
SPINNER_FRAMES = ("-", "\\", "|", "/")
WAVE_FRAMES = ("[    ]", "[=   ]", "[==  ]", "[=== ]", "[ ===]", "[  ==]", "[   =]")
SCAN_FRAMES = (" .    ", " ..   ", " ...  ", "  ... ", "   .. ", "    . ")


@dataclass
class Episode:
    title: str
    audio_url: str
    pub_date: Optional[datetime] = None
    description: str = ""


class FeedError(RuntimeError):
    pass


class FeedClient:
    def fetch_episodes(self) -> List[Episode]:
        raw = self._fetch_feed_bytes()
        return self._parse(raw)

    def _fetch_feed_bytes(self) -> bytes:
        last_error = None
        for url in RSS_CANDIDATES:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    return resp.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
        raise FeedError(f"Failed to fetch RSS feed: {last_error}")

    def _parse(self, xml_bytes: bytes) -> List[Episode]:
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError as exc:
            raise FeedError(f"Invalid RSS XML: {exc}") from exc

        channel = root.find("channel")
        if channel is None:
            raise FeedError("RSS channel not found")

        episodes: List[Episode] = []
        for item in channel.findall("item"):
            title = (item.findtext("title") or "Untitled").strip()
            description = (item.findtext("description") or "").strip()
            pub_date = self._parse_pub_date(item.findtext("pubDate"))
            enclosure = item.find("enclosure")
            audio_url = ""
            if enclosure is not None:
                audio_url = (enclosure.get("url") or "").strip()

            # Fallback: look for an explicit audio link if enclosure is missing.
            if not audio_url:
                for link in item.findall("link"):
                    candidate = (link.text or "").strip()
                    if candidate.endswith((".mp3", ".m4a", ".aac", ".ogg")):
                        audio_url = candidate
                        break

            if not audio_url:
                continue

            episodes.append(
                Episode(
                    title=title,
                    audio_url=audio_url,
                    pub_date=pub_date,
                    description=self._clean_text(description),
                )
            )

        if not episodes:
            raise FeedError("No playable episodes found in RSS")

        episodes.sort(key=lambda ep: ep.pub_date or datetime.min, reverse=True)
        return episodes

    def _parse_pub_date(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None

    def _clean_text(self, s: str) -> str:
        s = s.replace("\n", " ").replace("\r", " ")
        return " ".join(s.split())


class Player:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._mode = self._detect_mode()
        self._temp_file: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        return self._mode

    def _detect_mode(self) -> str:
        if shutil.which("mpv"):
            return "mpv"
        if shutil.which("afplay"):
            return "afplay"
        raise RuntimeError("No player found. Install 'mpv' or use macOS 'afplay'.")

    def is_playing(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def play(self, url: str) -> None:
        with self._lock:
            self.stop_locked()
            if self._mode == "mpv":
                self._proc = subprocess.Popen(
                    [
                        "mpv",
                        "--no-terminal",
                        "--really-quiet",
                        "--force-window=no",
                        url,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
                return

            # afplay works reliably with local files, so cache stream into a temp file.
            self._temp_file = self._download_temp(url)
            self._proc = subprocess.Popen(
                ["afplay", self._temp_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )

    def stop(self) -> None:
        with self._lock:
            self.stop_locked()

    def stop_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self._proc = None

        if self._temp_file and os.path.exists(self._temp_file):
            try:
                os.remove(self._temp_file)
            except OSError:
                pass
        self._temp_file = None

    def consume_finished(self) -> bool:
        """Return True when a track reached EOF since the last check."""
        with self._lock:
            if self._proc is None:
                return False
            if self._proc.poll() is None:
                return False
            self._proc = None
            if self._temp_file and os.path.exists(self._temp_file):
                try:
                    os.remove(self._temp_file)
                except OSError:
                    pass
            self._temp_file = None
            return True

    def _download_temp(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        suffix = os.path.splitext(parsed.path)[1] or ".mp3"
        fd, temp_path = tempfile.mkstemp(prefix="mfp_", suffix=suffix)
        os.close(fd)

        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp, open(temp_path, "wb") as out:
            shutil.copyfileobj(resp, out)
        return temp_path


class App:
    def __init__(self, stdscr: "curses._CursesWindow") -> None:
        self.stdscr = stdscr
        self.client = FeedClient()
        self.player = Player()

        self.episodes: List[Episode] = []
        self.index = 0
        self.message = "Loading feed..."
        self.now_playing: Optional[str] = None
        self.now_playing_index: Optional[int] = None
        self.autoplay = True
        self.shuffle = False
        self.loop_mode = "off"  # off | all | one
        self.tick = 0
        self.has_colors = False

    def run(self) -> None:
        curses.curs_set(0)
        self._init_theme()
        self.stdscr.nodelay(True)
        self.stdscr.timeout(200)
        self.stdscr.keypad(True)

        self.refresh_feed(initial=True)

        while True:
            self.tick += 1
            self._handle_track_end()
            self.render()
            key = self.stdscr.getch()
            if key == -1:
                continue

            if key in (ord("q"), 27):  # q / ESC
                break
            if key in (ord("j"), curses.KEY_DOWN):
                self.index = min(self.index + 1, max(0, len(self.episodes) - 1))
            elif key in (ord("k"), curses.KEY_UP):
                self.index = max(0, self.index - 1)
            elif key in (ord("g"),):
                self.index = 0
            elif key in (ord("G"),):
                self.index = max(0, len(self.episodes) - 1)
            elif key in (ord("r"),):
                self.refresh_feed(initial=False)
            elif key in (ord("s"),):
                self.player.stop()
                self.now_playing = None
                self.now_playing_index = None
                self.message = "Stopped"
            elif key in (ord("a"),):
                self.autoplay = not self.autoplay
                state = "ON" if self.autoplay else "OFF"
                self.message = f"Autoplay: {state}"
            elif key in (ord("x"),):
                self.shuffle = not self.shuffle
                state = "ON" if self.shuffle else "OFF"
                self.message = f"Shuffle: {state}"
            elif key in (ord("l"),):
                self.loop_mode = self._next_loop_mode(self.loop_mode)
                self.message = f"Loop: {self.loop_mode}"
            elif key in (10, 13, curses.KEY_ENTER):
                self.play_selected()

        self.player.stop()

    def refresh_feed(self, initial: bool) -> None:
        try:
            self.episodes = self.client.fetch_episodes()
            self.index = min(self.index, max(0, len(self.episodes) - 1))
            prefix = "Loaded" if initial else "Refreshed"
            self.message = f"{prefix} {len(self.episodes)} episodes"
        except Exception as exc:  # noqa: BLE001
            self.message = f"Feed error: {exc}"

    def play_selected(self) -> None:
        if not self.episodes:
            self.message = "No episode to play"
            return

        self._play_index(self.index)

    def _play_index(self, index: int, prefix: str = "Playing") -> None:
        ep = self.episodes[index]
        self.message = f"{prefix}: {ep.title}"
        self.now_playing = ep.title
        self.now_playing_index = index
        try:
            self.player.play(ep.audio_url)
        except Exception as exc:  # noqa: BLE001
            self.message = f"Playback error: {exc}"
            self.now_playing = None
            self.now_playing_index = None

    def _handle_track_end(self) -> None:
        if not self.player.consume_finished():
            return
        if self.now_playing is None:
            return

        finished_title = self.now_playing
        next_index = self._resolve_next_index()
        if next_index is None:
            self.now_playing = None
            self.now_playing_index = None
            self.message = f"Finished: {finished_title}"
            return
        self.index = next_index
        self._play_index(next_index, prefix="Auto")

    def _resolve_next_index(self) -> Optional[int]:
        if not self.episodes:
            return None
        if not self.autoplay:
            return None
        if self.now_playing_index is None:
            return None
        if self.loop_mode == "one":
            return self.now_playing_index
        if self.shuffle:
            if len(self.episodes) == 1:
                return 0
            pool = [i for i in range(len(self.episodes)) if i != self.now_playing_index]
            return random.choice(pool)

        next_index = self.now_playing_index + 1
        if next_index < len(self.episodes):
            return next_index
        if self.loop_mode == "all":
            return 0
        return None

    def _next_loop_mode(self, current: str) -> str:
        modes = ("off", "all", "one")
        try:
            idx = modes.index(current)
        except ValueError:
            return "off"
        return modes[(idx + 1) % len(modes)]

    def render(self) -> None:
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()

        if h < 16 or w < 72:
            self._render_compact(h, w)
            self.stdscr.refresh()
            return

        scan = SCAN_FRAMES[self.tick % len(SCAN_FRAMES)]
        self._fill_line(0, " ", self._attr("banner_bg"))
        self._fill_line(1, " ", self._attr("panel_bg"))
        self._addnstr(0, 2, "MUSIC FOR PROGRAMMING", w - 4, self._attr("banner_text"))
        self._addnstr(
            1,
            2,
            f"stream matrix{scan} player:{self.player.mode} episodes:{len(self.episodes)}",
            w - 4,
            self._attr("panel_bg"),
        )

        body_top = 3
        body_bottom = h - 3
        body_height = body_bottom - body_top + 1
        left_w = max(36, min(w - 30, int(w * 0.62)))
        right_x = left_w + 1
        right_w = w - right_x

        self._draw_box(0, body_top, left_w, body_height, "PLAYLIST")
        self._draw_box(right_x, body_top, right_w, body_height, "DETAIL")

        self._render_playlist(1, body_top + 1, left_w - 2, body_height - 2)
        self._render_detail(right_x + 1, body_top + 1, right_w - 2, body_height - 2)

        status = self.message
        if self.now_playing and self.player.is_playing():
            spin = SPINNER_FRAMES[self.tick % len(SPINNER_FRAMES)]
            status = f"{spin} now playing: {self.now_playing}"
        self._fill_line(h - 2, " ", self._attr("footer"))
        self._addnstr(h - 2, 1, textwrap.shorten(status, width=max(20, w - 3), placeholder="..."), w - 2, self._attr("footer"))
        self._addnstr(
            h - 1,
            1,
            "j/k or arrows move  Enter play  s stop  a autoplay  x shuffle  l loop  r refresh  q quit",
            w - 2,
            self._attr("muted"),
        )
        self.stdscr.refresh()

    def _render_compact(self, h: int, w: int) -> None:
        title = "MFP TUI"
        self._addnstr(0, 0, title, w - 1, self._attr("banner_text"))
        self._addnstr(1, 0, "-" * max(1, w - 1), w - 1, self._attr("muted"))

        list_top = 2
        list_bottom = max(list_top, h - 4)
        visible = max(1, list_bottom - list_top + 1)
        start = max(0, self.index - visible + 1)

        for row, ep_idx in enumerate(range(start, min(start + visible, len(self.episodes)))):
            ep = self.episodes[ep_idx]
            marker = ">" if ep_idx == self.index else " "
            line = f"{marker} {ep.title}"
            attr = self._attr("selected") if ep_idx == self.index else 0
            self._addnstr(list_top + row, 0, line, w - 1, attr)

        status = self.message
        if self.now_playing and self.player.is_playing():
            status = f"{SPINNER_FRAMES[self.tick % len(SPINNER_FRAMES)]} {self.now_playing}"
        self._addnstr(h - 1, 0, textwrap.shorten(status, width=max(10, w - 1), placeholder="..."), w - 1, self._attr("footer"))

    def _render_playlist(self, x: int, y: int, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        visible = max(1, h)
        start = 0
        if self.index >= visible:
            start = self.index - visible + 1

        playing = self.player.is_playing()
        for row, ep_idx in enumerate(range(start, min(start + visible, len(self.episodes)))):
            ep = self.episodes[ep_idx]
            selected = ep_idx == self.index
            is_playing = playing and ep_idx == self.now_playing_index
            date_txt = ep.pub_date.strftime("%y-%m-%d") if ep.pub_date else "--/--/--"
            pfx = ">" if selected else " "
            play_mark = "*" if is_playing else " "
            body = f"{pfx}{play_mark} {ep_idx + 1:03d} {date_txt}  {ep.title}"
            attr = self._attr("stripe_a") if row % 2 == 0 else self._attr("stripe_b")
            if selected:
                attr = self._attr("selected")
            elif is_playing:
                attr = self._attr("playing")
            self._addnstr(y + row, x, body, w, attr)

    def _render_detail(self, x: int, y: int, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        if not self.episodes:
            self._addnstr(y, x, "No episode loaded", w, self._attr("muted"))
            return

        current = self.episodes[self.index]
        playing = self.player.is_playing() and self.now_playing_index is not None
        spin = SPINNER_FRAMES[self.tick % len(SPINNER_FRAMES)] if playing else " "
        wave = WAVE_FRAMES[self.tick % len(WAVE_FRAMES)] if playing else "[    ]"
        auto_txt = "ON" if self.autoplay else "OFF"
        shuf_txt = "ON" if self.shuffle else "OFF"
        loop_txt = self.loop_mode.upper()

        self._addnstr(y + 0, x, f"{spin} NOW PLAYING", w, self._attr("accent"))
        now_playing = self.now_playing if playing and self.now_playing else "(idle)"
        self._addnstr(y + 1, x, textwrap.shorten(now_playing, width=max(12, w), placeholder="..."), w, self._attr("playing"))
        self._addnstr(y + 2, x, f"Signal {wave}", w, self._attr("muted"))
        self._addnstr(y + 4, x, textwrap.shorten(current.title, width=max(12, w), placeholder="..."), w, curses.A_BOLD)

        desc_width = max(12, w)
        desc_lines = textwrap.wrap(current.description or "(no description)", width=desc_width)
        max_desc = max(1, h - 11)
        for i in range(min(max_desc, len(desc_lines))):
            self._addnstr(y + 6 + i, x, desc_lines[i], w, 0)

        self._addnstr(y + h - 4, x, f"[A] AUTO {auto_txt}", w, self._attr("toggle_on") if self.autoplay else self._attr("toggle_off"))
        self._addnstr(y + h - 3, x, f"[X] SHUFFLE {shuf_txt}", w, self._attr("toggle_on") if self.shuffle else self._attr("toggle_off"))
        loop_attr = self._attr("toggle_on") if self.loop_mode != "off" else self._attr("toggle_off")
        self._addnstr(y + h - 2, x, f"[L] LOOP {loop_txt}", w, loop_attr)
        self._addnstr(y + h - 1, x, "[S] STOP  [R] REFRESH", w, self._attr("muted"))

    def _draw_box(self, x: int, y: int, w: int, h: int, title: str) -> None:
        if w < 2 or h < 2:
            return
        self.stdscr.hline(y, x + 1, curses.ACS_HLINE, max(0, w - 2))
        self.stdscr.hline(y + h - 1, x + 1, curses.ACS_HLINE, max(0, w - 2))
        self.stdscr.vline(y + 1, x, curses.ACS_VLINE, max(0, h - 2))
        self.stdscr.vline(y + 1, x + w - 1, curses.ACS_VLINE, max(0, h - 2))
        self.stdscr.addch(y, x, curses.ACS_ULCORNER)
        self.stdscr.addch(y, x + w - 1, curses.ACS_URCORNER)
        self.stdscr.addch(y + h - 1, x, curses.ACS_LLCORNER)
        self.stdscr.addch(y + h - 1, x + w - 1, curses.ACS_LRCORNER)
        self._addnstr(y, x + 2, f" {title} ", max(1, w - 4), self._attr("accent"))

    def _fill_line(self, y: int, ch: str, attr: int) -> None:
        _, w = self.stdscr.getmaxyx()
        if y < 0:
            return
        try:
            self.stdscr.addnstr(y, 0, ch * max(1, w - 1), w - 1, attr)
        except curses.error:
            pass

    def _init_theme(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        self.has_colors = True
        if curses.COLORS >= 256:
            curses.init_pair(1, 16, 45)    # banner bg
            curses.init_pair(2, 51, -1)    # accent cyan
            curses.init_pair(3, 16, 226)   # selected
            curses.init_pair(4, 118, -1)   # playing
            curses.init_pair(5, 111, -1)   # muted text
            curses.init_pair(6, 16, 39)    # footer
            curses.init_pair(7, 190, -1)   # toggle on
            curses.init_pair(8, 246, -1)   # toggle off
            curses.init_pair(9, 252, 235)  # stripe a
            curses.init_pair(10, 252, 237) # stripe b
            curses.init_pair(11, 39, -1)   # panel bg text
            return

        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_YELLOW)
        curses.init_pair(4, curses.COLOR_GREEN, -1)
        curses.init_pair(5, curses.COLOR_BLUE, -1)
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(7, curses.COLOR_YELLOW, -1)
        curses.init_pair(8, curses.COLOR_WHITE, -1)
        curses.init_pair(9, curses.COLOR_WHITE, -1)
        curses.init_pair(10, curses.COLOR_BLACK, -1)
        curses.init_pair(11, curses.COLOR_CYAN, -1)

    def _attr(self, role: str) -> int:
        if not self.has_colors:
            fallback = {
                "banner_text": curses.A_BOLD,
                "selected": curses.A_REVERSE | curses.A_BOLD,
                "footer": curses.A_REVERSE,
                "accent": curses.A_BOLD,
                "playing": curses.A_BOLD,
                "toggle_on": curses.A_BOLD,
                "toggle_off": 0,
                "muted": curses.A_DIM,
                "banner_bg": curses.A_REVERSE,
                "stripe_a": 0,
                "stripe_b": curses.A_DIM,
                "panel_bg": curses.A_BOLD,
            }
            return fallback.get(role, 0)

        mapping = {
            "banner_bg": curses.color_pair(1),
            "banner_text": curses.color_pair(1) | curses.A_BOLD,
            "accent": curses.color_pair(2) | curses.A_BOLD,
            "selected": curses.color_pair(3) | curses.A_BOLD,
            "playing": curses.color_pair(4) | curses.A_BOLD,
            "muted": curses.color_pair(5),
            "footer": curses.color_pair(6) | curses.A_BOLD,
            "toggle_on": curses.color_pair(7) | curses.A_BOLD,
            "toggle_off": curses.color_pair(8),
            "stripe_a": curses.color_pair(9),
            "stripe_b": curses.color_pair(10),
            "panel_bg": curses.color_pair(11) | curses.A_BOLD,
        }
        return mapping.get(role, 0)

    def _addnstr(self, y: int, x: int, s: str, n: int, attr: int = 0) -> None:
        if y < 0 or x < 0 or n <= 0:
            return
        try:
            self.stdscr.addnstr(y, x, s, n, attr)
        except curses.error:
            pass


def main() -> int:
    try:
        curses.wrapper(lambda stdscr: App(stdscr).run())
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
