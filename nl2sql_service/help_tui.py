from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from typing import Sequence

from nl2sql_service import help_docs
from nl2sql_service.help_docs import HelpEndpoint, HelpIndex


DEFAULT_BASE_URL = "http://localhost:8080"


def load_help_index() -> HelpIndex:
    from nl2sql_service.main import app

    return help_docs.build_help_index(app.openapi())


def filter_endpoints(
    index: HelpIndex,
    *,
    module: str | None = None,
    search: str | None = None,
) -> tuple[HelpEndpoint, ...]:
    endpoints = index.by_module.get(module, ()) if module else index.endpoints
    query = (search or "").strip().lower()
    if not query:
        return tuple(endpoints)
    return tuple(endpoint for endpoint in endpoints if query in help_docs.endpoint_search_text(endpoint))


def resolve_route(index: HelpIndex, route: str) -> HelpEndpoint | None:
    module, sep, slug = route.partition("/")
    if sep:
        return index.by_detail.get((module, slug))

    candidates = [
        endpoint
        for endpoint in index.endpoints
        if endpoint.slug == route or endpoint.path == route or endpoint.path.lstrip("/") == route.lstrip("/")
    ]
    return candidates[0] if len(candidates) == 1 else None


def render_route_list(
    index: HelpIndex,
    *,
    module: str | None = None,
    search: str | None = None,
) -> str:
    endpoints = filter_endpoints(index, module=module, search=search)
    title = "NL2SQL Terminal Help"
    if module:
        title += f" - {help_docs.MODULE_LABELS.get(module, module.title())}"
    if search:
        title += f" - search: {search}"

    lines = [
        title,
        "=" * len(title),
        "",
        "Modules: " + ", ".join(help_docs.MODULE_LABELS),
        f"Routes: {len(endpoints)}",
        "",
    ]
    for endpoint in endpoints:
        lines.append(f"[{endpoint.module}] {endpoint.method:<6} {endpoint.path:<32} {endpoint.title}")
        lines.append(f"  {endpoint.summary}")
    return "\n".join(lines).rstrip() + "\n"


def render_route_detail(index: HelpIndex, endpoint: HelpEndpoint, *, base_url: str = DEFAULT_BASE_URL) -> str:
    module_label = help_docs.MODULE_LABELS.get(endpoint.module, endpoint.module.title())
    lines = [
        endpoint.title,
        "=" * len(endpoint.title),
        "",
        f"Route: {endpoint.method} {endpoint.path}",
        f"Module: {module_label} ({endpoint.module})",
        "",
        "Purpose",
        "-------",
        endpoint.summary,
        "",
        "Description",
        "-----------",
        _wrap(endpoint.description),
        "",
        "Parameters",
        "----------",
        _parameters_text(endpoint),
        "",
        "Request Body",
        "------------",
        help_docs.request_body_schema_label(endpoint),
        _json_example(endpoint.request_example, empty="No request body."),
        "",
        "Response",
        "--------",
        help_docs.response_schema_label(endpoint),
        _json_example(endpoint.response_example, empty="No sample response."),
        "",
        "Error Responses",
        "---------------",
        _bullet_list(endpoint.error_cases or ("HTTP 422 for invalid request shape when applicable.",)),
        "",
        "Authentication",
        "--------------",
        endpoint.auth,
        "",
        "Curl",
        "----",
        help_docs.curl_command(endpoint, base_url),
        "",
        "Related Routes",
        "--------------",
        _related_text(index, endpoint),
    ]
    if endpoint.notes:
        lines.extend(["", "Notes", "-----", _bullet_list(endpoint.notes)])
    return "\n".join(lines).rstrip() + "\n"


def run_interactive(index: HelpIndex, *, base_url: str = DEFAULT_BASE_URL) -> int:
    if not _supports_curses():
        print(render_route_list(index), end="")
        return 0

    try:
        import curses
    except Exception as exc:  # noqa: BLE001
        print(f"Interactive terminal rendering is unavailable: {exc}", file=sys.stderr)
        print(render_route_list(index), end="")
        return 0

    try:
        curses.wrapper(lambda stdscr: _HelpApp(stdscr, index, base_url).run())
    except Exception as exc:  # noqa: BLE001
        print(f"Interactive terminal rendering failed: {exc}", file=sys.stderr)
        print(render_route_list(index), end="")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Browse NL2SQL route help from the terminal")
    parser.add_argument("--module", choices=tuple(help_docs.MODULE_LABELS), help="Show routes for one module")
    parser.add_argument("--route", help="Show one route detail, for example generation/ask")
    parser.add_argument("--search", help="Filter routes by path, method, module, title, or description")
    parser.add_argument("--plain", action="store_true", help="Use non-interactive plain text output")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Base URL used in generated curl examples")
    args = parser.parse_args(argv)

    index = load_help_index()

    if args.route:
        endpoint = resolve_route(index, args.route)
        if endpoint is None:
            print(f"Route help not found: {args.route}", file=sys.stderr)
            return 2
        print(render_route_detail(index, endpoint, base_url=args.base_url), end="")
        return 0

    if args.plain or args.module or args.search or not _interactive_stdio():
        print(render_route_list(index, module=args.module, search=args.search), end="")
        return 0

    return run_interactive(index, base_url=args.base_url)


class _HelpApp:
    def __init__(self, stdscr, index: HelpIndex, base_url: str) -> None:
        self.stdscr = stdscr
        self.index = index
        self.base_url = base_url
        self.module: str | None = None
        self.search = ""
        self.selected = 0
        self.scroll = 0
        self.detail: HelpEndpoint | None = None

    def run(self) -> None:
        import curses

        curses.curs_set(0)
        self.stdscr.keypad(True)
        while True:
            self._draw()
            key = self.stdscr.getch()
            if key in (ord("q"), 27):
                return
            if self.detail is not None:
                if key in (ord("b"), curses.KEY_BACKSPACE, 127):
                    self.detail = None
                    self.scroll = 0
                elif key in (curses.KEY_UP, ord("k")):
                    self.scroll = max(0, self.scroll - 1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    self.scroll += 1
                continue

            if key in (curses.KEY_UP, ord("k")):
                self.selected = max(0, self.selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                endpoints = self._visible_endpoints()
                self.selected = min(max(0, len(endpoints) - 1), self.selected + 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                endpoints = self._visible_endpoints()
                if endpoints:
                    self.detail = endpoints[self.selected]
                    self.scroll = 0
            elif key == ord("/"):
                self._prompt_search()
            elif key == ord("b"):
                self.module = None
                self.search = ""
                self.selected = 0
            elif key == ord("a"):
                self.module = None
                self.selected = 0
            elif ord("1") <= key <= ord("5"):
                modules = tuple(help_docs.MODULE_LABELS)
                self.module = modules[key - ord("1")]
                self.selected = 0

    def _visible_endpoints(self) -> tuple[HelpEndpoint, ...]:
        return filter_endpoints(self.index, module=self.module, search=self.search)

    def _draw(self) -> None:
        self.stdscr.erase()
        if self.detail is not None:
            self._draw_detail()
        else:
            self._draw_list()
        self.stdscr.refresh()

    def _draw_list(self) -> None:
        import curses

        height, width = self.stdscr.getmaxyx()
        endpoints = self._visible_endpoints()
        module_text = "  ".join(
            ["a:all", *[f"{idx + 1}:{name}" for idx, name in enumerate(help_docs.MODULE_LABELS)]]
        )
        title = "NL2SQL Help TUI"
        active = self.module or "all"
        search = self.search or "-"
        self._add(0, 0, f"{title} | module={active} | search={search}", curses.A_BOLD)
        self._add(1, 0, module_text[: max(0, width - 1)])
        self._add(2, 0, "Keys: up/down or j/k, Enter=open, /=search, b=reset, q=quit")
        self._add(3, 0, "-" * max(0, width - 1))

        if not endpoints:
            self._add(5, 0, "No routes match the current filter.")
            return

        self.selected = min(self.selected, len(endpoints) - 1)
        visible_height = max(1, height - 5)
        if self.selected < self.scroll:
            self.scroll = self.selected
        elif self.selected >= self.scroll + visible_height:
            self.scroll = self.selected - visible_height + 1

        for row, endpoint in enumerate(endpoints[self.scroll : self.scroll + visible_height], start=4):
            absolute = self.scroll + row - 4
            prefix = ">" if absolute == self.selected else " "
            text = f"{prefix} [{endpoint.module}] {endpoint.method:<6} {endpoint.path:<30} {endpoint.title}"
            attr = curses.A_REVERSE if absolute == self.selected else curses.A_NORMAL
            self._add(row, 0, text[: max(0, width - 1)], attr)

    def _draw_detail(self) -> None:
        import curses

        assert self.detail is not None
        height, width = self.stdscr.getmaxyx()
        detail_text = render_route_detail(self.index, self.detail, base_url=self.base_url)
        lines = detail_text.splitlines()
        self._add(0, 0, "Route Detail | up/down or j/k scroll, b=back, q=quit", curses.A_BOLD)
        self._add(1, 0, "-" * max(0, width - 1))
        content_height = max(1, height - 2)
        max_scroll = max(0, len(lines) - content_height)
        self.scroll = min(self.scroll, max_scroll)
        for row, line in enumerate(lines[self.scroll : self.scroll + content_height], start=2):
            self._add(row, 0, line[: max(0, width - 1)])

    def _prompt_search(self) -> None:
        import curses

        height, width = self.stdscr.getmaxyx()
        prompt = "Search: "
        curses.echo()
        curses.curs_set(1)
        self._add(height - 1, 0, " " * max(0, width - 1))
        self._add(height - 1, 0, prompt)
        self.stdscr.refresh()
        try:
            raw = self.stdscr.getstr(height - 1, len(prompt), max(1, width - len(prompt) - 1))
            self.search = raw.decode("utf-8", "replace").strip()
            self.selected = 0
            self.scroll = 0
        finally:
            curses.noecho()
            curses.curs_set(0)

    def _add(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.stdscr.getmaxyx()
        if y >= height or x >= width:
            return
        self.stdscr.addstr(y, x, text[: max(0, width - x - 1)], attr)


def _parameters_text(endpoint: HelpEndpoint) -> str:
    rows = help_docs.parameter_rows(endpoint)
    if not rows:
        return "No path or query parameters."
    lines = []
    for row in rows:
        suffix = f" - {row['description']}" if row["description"] else ""
        lines.append(
            f"- {row['name']} ({row['location']}, {row['required']}, {row['type']}){suffix}"
        )
    return "\n".join(lines)


def _related_text(index: HelpIndex, endpoint: HelpEndpoint) -> str:
    if not endpoint.related:
        return "No related routes listed."
    lines = []
    for path in endpoint.related:
        matches = [candidate for candidate in index.endpoints if candidate.path == path]
        if matches:
            for match in matches:
                lines.append(f"- {match.method} {match.path} ({match.module}/{match.slug})")
        else:
            lines.append(f"- {path}")
    return "\n".join(lines)


def _json_example(value: object | None, *, empty: str) -> str:
    if value is None:
        return empty
    return json.dumps(value, indent=2, default=str)


def _bullet_list(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _wrap(text: str) -> str:
    return "\n".join(textwrap.wrap(text, width=88)) or text


def _interactive_stdio() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _supports_curses() -> bool:
    return _interactive_stdio() and os.environ.get("TERM", "dumb") not in {"", "dumb"}


if __name__ == "__main__":
    raise SystemExit(main())
