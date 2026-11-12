"""Command line interface for StockForge (SF-001)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from stockforge.config import Settings, get_settings
from stockforge.logging import mask_secret, setup_logging

app = typer.Typer(
    name="stockforge",
    help="StockForge: AI-powered commercial stock photography asset generation pipeline.",
    add_completion=False,
)
console = Console()


DATA_SUBDIRECTORIES = ["raw", "processed", "thumbnails", "exports", "backups", "logs"]


@app.command(name="init")
def init(
    data_dir: Optional[str] = typer.Option(
        None,
        "--data-dir",
        "-d",
        help="Target data storage directory. Defaults to DATA_DIR from settings or ./data.",
    ),
) -> None:
    """Initialize local directory hierarchy and default configuration files."""
    settings = get_settings()
    target_path = Path(data_dir or settings.data_dir).resolve()

    console.print(f"[bold cyan]Memulai inisialisasi direktori StockForge di:[/bold cyan] {target_path}")

    # Create subdirectories
    created: List[Path] = []
    try:
        target_path.mkdir(parents=True, exist_ok=True)
        for subdir in DATA_SUBDIRECTORIES:
            subpath = target_path / subdir
            subpath.mkdir(parents=True, exist_ok=True)
            created.append(subpath)

        # Test write permission
        test_file = target_path / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
    except Exception as e:
        console.print(f"[bold red]GAGAL membuat atau menulis ke direktori data:[/bold red] {e}")
        raise typer.Exit(code=1)

    # Ensure config dir exists
    config_dir = Path("config").resolve()
    config_dir.mkdir(parents=True, exist_ok=True)

    table = Table(title="Struktur Direktori Data StockForge", show_header=True)
    table.add_column("Subdirektori", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Path", style="dim")

    for subpath in created:
        table.add_row(subpath.name, "✓ Siap", str(subpath))

    console.print(table)
    console.print(
        Panel.fit(
            "[bold green]Inisialisasi berhasil![/bold green]\n"
            "Direktori penyimpanan telah siap dengan izin akses tulis.",
            border_style="green",
        )
    )


@app.command(name="doctor")
def doctor() -> None:
    """Inspect environment configuration, secrets status, storage and security invariants."""
    console.print("[bold cyan]Menjalankan StockForge Doctor...[/bold cyan]\n")

    try:
        settings = get_settings()
    except Exception as e:
        console.print(f"[bold red]Gagal memuat konfigurasi Settings:[/bold red] {e}")
        raise typer.Exit(code=1)

    issues_found = False

    # 1. Environment & Python info
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 12)
    if not py_ok:
        issues_found = True

    # 2. Table: System & Environment
    env_table = Table(title="1. Lingkungan & Runtime", show_header=True)
    env_table.add_column("Item", style="cyan")
    env_table.add_column("Nilai", style="white")
    env_table.add_column("Status", style="bold")

    env_table.add_row(
        "Python Version",
        py_ver,
        "[green]OK[/green]" if py_ok else "[red]Perlu >= 3.12[/red]",
    )
    env_table.add_row("APP_ENV", settings.app_env, "[green]OK[/green]")
    env_table.add_row("Timezone (TZ)", settings.tz, "[green]OK[/green]")
    env_table.add_row("Database URL", settings.database_url, "[green]OK[/green]")
    console.print(env_table)
    console.print()

    # 3. Table: Secrets & Credentials (Safely masked; missing secrets reported without printing values)
    sec_table = Table(title="2. Status Kredensial & Secrets (Masked)", show_header=True)
    sec_table.add_column("Kredensial", style="cyan")
    sec_table.add_column("Status", style="white")
    sec_table.add_column("Masked Value / Detail", style="dim")

    credentials = [
        ("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token),
        ("RESEARCH_API_KEY", settings.research_api_key),
        ("PROMPT_API_KEY", settings.prompt_api_key),
        ("IMAGE_API_KEY", settings.image_api_key),
        ("VISION_QC_API_KEY", settings.vision_qc_api_key),
        ("METADATA_API_KEY", settings.metadata_api_key),
    ]

    for name, secret_val in credentials:
        raw_val = secret_val.get_secret_value() if secret_val else ""
        if raw_val:
            sec_table.add_row(name, "[green]TERPASANG[/green]", mask_secret(raw_val, show_last=4))
        else:
            sec_table.add_row(name, "[yellow]BELUM DIISI[/yellow]", "Tidak ada nilai (opsional / mock mode)")

    # Admin IDs
    admin_count = len(settings.admin_ids_list)
    if admin_count > 0:
        sec_table.add_row("TELEGRAM_ADMIN_IDS", "[green]TERPASANG[/green]", f"{admin_count} ID admin terdaftar")
    else:
        sec_table.add_row("TELEGRAM_ADMIN_IDS", "[yellow]BELUM DIISI[/yellow]", "Bot tidak akan mengizinkan akses siapapun")

    console.print(sec_table)
    console.print()

    # 4. Table: Storage & Data Directories
    storage_table = Table(title="3. Validasi Direktori Penyimpanan", show_header=True)
    storage_table.add_column("Direktori", style="cyan")
    storage_table.add_column("Path", style="dim")
    storage_table.add_column("Status", style="bold")

    data_dir_path = Path(settings.data_dir).resolve()
    dir_exists = data_dir_path.exists()
    writable = False
    if dir_exists:
        try:
            test_file = data_dir_path / ".doctor_test"
            test_file.write_text("doctor")
            test_file.unlink()
            writable = True
        except Exception:
            writable = False

    status_str = "[green]Siap & Writable[/green]" if writable else (
        "[yellow]Direktori ada, tidak writable[/yellow]" if dir_exists else "[yellow]Belum diinisialisasi (Jalankan 'stockforge init')[/yellow]"
    )
    storage_table.add_row("DATA_DIR", str(data_dir_path), status_str)
    console.print(storage_table)
    console.print()

    # 5. Table: Security & Compliance Invariants
    inv_table = Table(title="4. Invarian Keamanan & Kepatuhan", show_header=True)
    inv_table.add_column("Invarian", style="cyan")
    inv_table.add_column("Nilai", style="white")
    inv_table.add_column("Hasil Pemeriksaan", style="bold")

    approval_ok = settings.require_human_approval is True
    inv_table.add_row(
        "REQUIRE_HUMAN_APPROVAL",
        str(settings.require_human_approval),
        "[green]Valid (Wajib persetujuan manusia)[/green]" if approval_ok else "[red]PELANGGARAN: Harus True[/red]",
    )

    auto_sub_ok = settings.allow_adobe_auto_submission is False
    inv_table.add_row(
        "ALLOW_ADOBE_AUTO_SUBMISSION",
        str(settings.allow_adobe_auto_submission),
        "[green]Valid (Auto-submission dilarang)[/green]" if auto_sub_ok else "[red]PELANGGARAN: Harus False[/red]",
    )

    console.print(inv_table)
    console.print()

    if not approval_ok or not auto_sub_ok:
        issues_found = True
        console.print("[bold red]Peringatan: Terdapat pelanggaran invarian kepatuhan![/bold red]")
        raise typer.Exit(code=1)

    console.print("[bold green]Pemeriksaan StockForge Doctor selesai. Semua konfigurasi dasar dan invarian valid.[/bold green]")


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()

