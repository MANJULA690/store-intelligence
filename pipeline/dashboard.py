"""
dashboard.py — Live terminal dashboard for Store Intelligence.
Updates every 5 seconds with real metrics from the API.

Usage:
    python pipeline/dashboard.py --store ST1008 --api http://localhost:8000

Requires: pip install rich httpx
"""

import argparse
import time
import sys
from datetime import datetime, timezone

try:
    import httpx
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.live import Live
    from rich.text import Text
    from rich import box
except ImportError:
    print("Install dashboard dependencies: pip install rich httpx")
    sys.exit(1)

console = Console()


def fetch(api_url: str, path: str) -> dict | None:
    try:
        r = httpx.get(f"{api_url}{path}", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        return None
    return None


def build_dashboard(store_id: str, api_url: str) -> Layout:
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    metrics   = fetch(api_url, f"/stores/{store_id}/metrics")
    funnel    = fetch(api_url, f"/stores/{store_id}/funnel")
    anomalies = fetch(api_url, f"/stores/{store_id}/anomalies")
    health    = fetch(api_url, "/health")

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=4),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )

    # ── Header ────────────────────────────────────────────────────────────
    api_status = "[green]● CONNECTED[/]" if metrics else "[red]● DISCONNECTED[/]"
    layout["header"].update(Panel(
        f"  [bold cyan]Store Intelligence Dashboard[/]  |  "
        f"Store: [yellow]{store_id}[/]  |  "
        f"{api_status}  |  [dim]{now}[/]",
        box=box.HORIZONTALS,
    ))

    # ── Left: Metrics + Funnel ────────────────────────────────────────────
    if metrics:
        conf_colour = {"HIGH": "green", "LOW": "yellow", "NO_DATA": "red"}.get(
            metrics.get("data_confidence", ""), "white"
        )
        m_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
        m_table.add_column("Metric", style="dim")
        m_table.add_column("Value", justify="right")

        m_table.add_row("Unique Visitors",   f"[bold]{metrics['unique_visitors']}[/]")
        m_table.add_row("Conversion Rate",   f"[bold green]{metrics['conversion_rate']:.1%}[/]")
        m_table.add_row("Avg Dwell",         f"{metrics['avg_dwell_ms'] // 1000}s")
        m_table.add_row("Queue Depth",       f"[bold {'red' if metrics['current_queue_depth'] >= 5 else 'white'}]{metrics['current_queue_depth']}[/]")
        m_table.add_row("Abandonment Rate",  f"{metrics['abandonment_rate']:.1%}")
        m_table.add_row("Data Confidence",   f"[{conf_colour}]{metrics['data_confidence']}[/]")

        metrics_panel = Panel(m_table, title="[bold]Metrics[/]", border_style="cyan")
    else:
        metrics_panel = Panel("[dim]No data yet. Run: ./pipeline/run.sh[/]",
                              title="[bold]Metrics[/]", border_style="dim")

    if funnel:
        f_table = Table(box=box.SIMPLE, padding=(0, 1))
        f_table.add_column("Stage")
        f_table.add_column("Count",    justify="right")
        f_table.add_column("Drop-off", justify="right")

        for stage in funnel["stages"]:
            drop_col = (
                "[red]" if stage["drop_off_pct"] > 40 else
                "[yellow]" if stage["drop_off_pct"] > 20 else
                "[green]"
            )
            f_table.add_row(
                stage["stage"],
                str(stage["count"]),
                f"{drop_col}{stage['drop_off_pct']:.1f}%[/]",
            )
        funnel_panel = Panel(f_table, title="[bold]Conversion Funnel[/]", border_style="cyan")
    else:
        funnel_panel = Panel("[dim]—[/]", title="[bold]Conversion Funnel[/]", border_style="dim")

    left_layout = Layout()
    left_layout.split_column(
        Layout(metrics_panel, name="metrics"),
        Layout(funnel_panel,  name="funnel"),
    )
    layout["left"].update(left_layout)

    # ── Right: Anomalies + Health ─────────────────────────────────────────
    if anomalies and anomalies["anomalies"]:
        a_table = Table(box=box.SIMPLE, padding=(0, 1))
        a_table.add_column("Type",     style="bold")
        a_table.add_column("Severity", justify="center")
        a_table.add_column("Action")

        sev_colour = {"CRITICAL": "red", "WARN": "yellow", "INFO": "blue"}
        for anom in anomalies["anomalies"][:5]:
            sc = sev_colour.get(anom["severity"], "white")
            a_table.add_row(
                anom["anomaly_type"],
                f"[{sc}]{anom['severity']}[/]",
                anom["suggested_action"][:50] + "…" if len(anom["suggested_action"]) > 50
                else anom["suggested_action"],
            )
        anom_panel = Panel(a_table, title="[bold]Active Anomalies[/]", border_style="yellow")
    else:
        anom_panel = Panel("[green]No active anomalies[/]",
                           title="[bold]Active Anomalies[/]", border_style="green")

    if health:
        h_colour = {"OK": "green", "DEGRADED": "yellow", "ERROR": "red"}.get(
            health["status"], "white"
        )
        h_text = Text()
        h_text.append(f"System: ", style="dim")
        h_text.append(f"{health['status']}\n", style=h_colour)
        for store in health.get("stores", []):
            sc = "green" if store["status"] == "OK" else "red"
            lag = f"lag={store['lag_seconds']}s" if store["lag_seconds"] is not None else "no events"
            h_text.append(f"  {store['store_id']}: ", style="dim")
            h_text.append(f"{store['status']} ({lag})\n", style=sc)
        health_panel = Panel(h_text, title="[bold]Health[/]", border_style="cyan")
    else:
        health_panel = Panel("[red]API unreachable[/]",
                             title="[bold]Health[/]", border_style="red")

    right_layout = Layout()
    right_layout.split_column(
        Layout(anom_panel,  name="anomalies"),
        Layout(health_panel, name="health"),
    )
    layout["right"].update(right_layout)

    # ── Footer ────────────────────────────────────────────────────────────
    layout["footer"].update(Panel(
        "[dim]Refreshes every 5s  |  Ctrl+C to exit  |  "
        f"API: {api_url}[/]",
        box=box.HORIZONTALS,
    ))

    return layout


def main():
    parser = argparse.ArgumentParser(description="Store Intelligence Live Dashboard")
    parser.add_argument("--store", default="ST1008")
    parser.add_argument("--api",   default="http://localhost:8000")
    parser.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()

    console.print(f"\n[cyan]Starting dashboard for store [bold]{args.store}[/] @ {args.api}[/]")
    console.print("[dim]Press Ctrl+C to exit[/]\n")

    with Live(
        build_dashboard(args.store, args.api),
        refresh_per_second=1,
        screen=True,
    ) as live:
        try:
            while True:
                time.sleep(args.interval)
                live.update(build_dashboard(args.store, args.api))
        except KeyboardInterrupt:
            console.print("\n[yellow]Dashboard stopped.[/]")


if __name__ == "__main__":
    main()
