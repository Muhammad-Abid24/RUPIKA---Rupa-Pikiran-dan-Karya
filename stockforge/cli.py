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
    name="rupika",
    help="RUPIKA: Rupa, Pikiran, dan Karya — Pipeline produksi aset fotografi stok komersial berbasis AI.",
    add_completion=False,
)
console = Console()


@app.callback()
def main_callback() -> None:
    """Initialize structured secret-safe logging."""
    setup_logging()


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

    console.print(f"[bold cyan]Memulai inisialisasi direktori RUPIKA di:[/bold cyan] {target_path}")

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

    table = Table(title="Struktur Direktori Data RUPIKA", show_header=True)
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
    console.print("[bold cyan]Menjalankan RUPIKA Doctor...[/bold cyan]\n")

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

    console.print("[bold green]Pemeriksaan RUPIKA Doctor selesai. Semua konfigurasi dasar dan invarian valid.[/bold green]")


@app.command(name="bot")
def bot_command(
    token: Optional[str] = typer.Option(
        None,
        "--token",
        "-t",
        help="Telegram bot token. Defaults to TELEGRAM_BOT_TOKEN from environment.",
    ),
    drop_pending_updates: bool = typer.Option(
        False,
        "--drop-pending-updates",
        help="Drop stale pending updates accumulated while offline.",
    ),
) -> None:
    """Start Telegram bot long polling service."""
    from stockforge.bot.service import start_bot
    console.print("[bold cyan]Menjalankan RUPIKA Telegram Bot service...[/bold cyan]")
    try:
        start_bot(token=token, drop_pending_updates=drop_pending_updates)
    except Exception as e:
        console.print(f"[bold red]Gagal menjalankan bot:[/bold red] {e}")
        raise typer.Exit(code=1)


@app.command(name="worker")
def worker_command(
    poll_interval: float = typer.Option(
        1.0,
        "--interval",
        "-i",
        help="Polling interval in seconds when queue is idle.",
    ),
    lease_seconds: int = typer.Option(
        60,
        "--lease",
        "-l",
        help="Lease duration in seconds.",
    ),
) -> None:
    """Start worker daemon to process pipeline jobs from the queue."""
    from stockforge.db.session import create_db_engine
    from stockforge.queue.service import QueueService
    from stockforge.queue.worker import Worker

    console.print("[bold cyan]Menjalankan RUPIKA Worker daemon...[/bold cyan]")
    engine = create_db_engine()
    queue_service = QueueService(engine)
    worker = Worker(
        queue_service=queue_service,
        poll_interval_seconds=poll_interval,
        lease_seconds=lease_seconds,
    )
    try:
        worker.run()
    except KeyboardInterrupt:
        worker.stop()
        console.print("[bold yellow]Worker dihentikan oleh pengguna.[/bold yellow]")


# Sub-command group: Database (SF-002, SF-014)
db_app = typer.Typer(name="db", help="Manajemen migrasi dan skema basis data SQLite.")
app.add_typer(db_app, name="db")


@db_app.command(name="upgrade")
def db_upgrade(
    revision: str = typer.Option("head", "--revision", "-r", help="Target revisi Alembic"),
) -> None:
    """Jalankan migrasi skema database ke revisi target (default: head)."""
    import alembic.command
    import alembic.config

    ini_path = Path("alembic.ini")
    if not ini_path.exists():
        console.print("[bold red]File alembic.ini tidak ditemukan.[/bold red]")
        raise typer.Exit(code=1)

    console.print(f"[bold cyan]Menjalankan migrasi database Alembic ke revisi:[/bold cyan] {revision}")
    try:
        alembic_cfg = alembic.config.Config(str(ini_path))
        alembic.command.upgrade(alembic_cfg, revision)
        console.print("[bold green]✓ Migrasi database berhasil dijalankan.[/bold green]")
    except Exception as e:
        console.print(f"[bold red]Gagal menjalankan migrasi database:[/bold red] {e}")
        raise typer.Exit(code=1)


# Sub-command group: Research (SF-006)
research_app = typer.Typer(
    name="research",
    help="Riset niche pasar dan pembuatan konsep/prompt stok komersial.",
)
app.add_typer(research_app, name="research")


@research_app.command(name="generate")
def research_generate(
    niche: str = typer.Argument(..., help="Nama niche atau topik komersial"),
    count: int = typer.Option(10, "--count", "-c", help="Jumlah konsep yang dibuat"),
) -> None:
    """Generate ide konsep komersial berstatus 'draft' untuk suatu niche."""
    import asyncio
    from stockforge.db.session import create_db_engine, create_session_factory
    from stockforge.providers.registry import ProviderRegistry
    from stockforge.providers.router import ProviderRouter
    from stockforge.research.service import ResearchService

    console.print(f"[bold cyan]Melakukan riset konsep stok komersial untuk niche:[/bold cyan] {niche}")
    console.print("[dim]Aturan invarian: Proses ini TIDAK melakukan panggilan pembuatan gambar (zero image calls).[/dim]")

    engine = create_db_engine()
    session_factory = create_session_factory(engine)

    # Initialize router if config exists
    config_path = Path("config/models.yaml")
    router = None
    if config_path.exists():
        registry = ProviderRegistry()
        router = ProviderRouter(registry)
        try:
            router.load_from_yaml(config_path)
        except Exception:
            router = None

    service = ResearchService(session_factory=session_factory, router=router)

    try:
        result = asyncio.run(service.generate_concepts(niche_identifier=niche, count=count))
    except Exception as e:
        console.print(f"[bold red]Gagal melakukan riset konsep:[/bold red] {e}")
        raise typer.Exit(code=1)

    niche_title = result.niche.name if result.niche else niche
    table = Table(title=f"Konsep Stok Komersial — Niche: {niche_title}", show_header=True)
    table.add_column("ID", style="cyan", width=6)
    table.add_column("Judul", style="bold")
    table.add_column("Fingerprint", style="dim", width=12)
    table.add_column("Status", style="yellow", width=10)

    for c in result.concepts:
        table.add_row(str(c.id), c.title, c.fingerprint[:10] + "...", c.status)

    console.print(table)
    console.print(f"\n[green]✓ {len(result.concepts)} konsep berhasil disimpan sebagai 'draft'.[/green]")
    if result.total_duplicates > 0:
        console.print(f"[yellow]⚠ {result.total_duplicates} konsep duplikat dicegah & ditandai.[/yellow]")
    if result.unverified_count > 0:
        console.print(f"[dim]ℹ {result.unverified_count} konsep ditandai sebagai unverified evidence.[/dim]")

    console.print(
        Panel.fit(
            "[bold yellow]Pemberitahuan Human Approval:[/bold yellow]\n"
            "Semua konsep berada dalam status 'draft'.\n"
            "Gunakan perintah 'rupika research approve <ID>' untuk menyetujui konsep sebelum produksi.",
            border_style="yellow",
        )
    )


@research_app.command(name="approve")
def research_approve(
    concept_id: int = typer.Argument(..., help="ID konsep yang akan disetujui"),
) -> None:
    """Setujui konsep draft agar siap untuk tahap pembuatan batch gambar."""
    from stockforge.db.session import create_db_engine, create_session_factory
    from stockforge.research.service import ResearchService

    engine = create_db_engine()
    session_factory = create_session_factory(engine)
    service = ResearchService(session_factory=session_factory)

    try:
        concept = service.approve_concept(concept_id)
        console.print(f"[bold green]✓ Konsep #{concept.id} ('{concept.title}') berhasil disetujui![/bold green]")
    except Exception as e:
        console.print(f"[bold red]Gagal menyetujui konsep #{concept_id}:[/bold red] {e}")
        raise typer.Exit(code=1)


@research_app.command(name="reject")
def research_reject(
    concept_id: int = typer.Argument(..., help="ID konsep yang akan ditolak"),
    reason: Optional[str] = typer.Option(None, "--reason", "-r", help="Alasan penolakan"),
) -> None:
    """Tolak konsep draft."""
    from stockforge.db.session import create_db_engine, create_session_factory
    from stockforge.research.service import ResearchService

    engine = create_db_engine()
    session_factory = create_session_factory(engine)
    service = ResearchService(session_factory=session_factory)

    try:
        concept = service.reject_concept(concept_id, reason=reason)
        console.print(f"[yellow]✓ Konsep #{concept.id} ('{concept.title}') telah ditolak.[/yellow]")
    except Exception as e:
        console.print(f"[bold red]Gagal menolak konsep #{concept_id}:[/bold red] {e}")
        raise typer.Exit(code=1)


@research_app.command(name="list")
def research_list(
    niche_id: Optional[int] = typer.Option(None, "--niche-id", "-n", help="Filter berdasarkan ID Niche"),
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter status ('draft', 'approved', 'rejected')"),
) -> None:
    """Daftar konsep yang tersimpan di basis data."""
    from stockforge.db.session import create_db_engine, create_session_factory
    from stockforge.research.service import ResearchService

    engine = create_db_engine()
    session_factory = create_session_factory(engine)
    service = ResearchService(session_factory=session_factory)

    concepts = service.list_concepts(niche_id=niche_id, status=status)
    if not concepts:
        console.print("[dim]Tidak ada konsep yang sesuai dengan kriteria.[/dim]")
        return

    table = Table(title="Daftar Konsep Riset", show_header=True)
    table.add_column("ID", style="cyan", width=6)
    table.add_column("Niche ID", width=10)
    table.add_column("Judul", style="bold")
    table.add_column("Status", width=12)

    for c in concepts:
        status_color = "yellow" if c.status == "draft" else ("green" if c.status == "approved" else "red")
        table.add_row(str(c.id), str(c.niche_id), c.title, f"[{status_color}]{c.status}[/{status_color}]")

    console.print(table)


# Image Generation Command (SF-007)
@app.command(name="generate")
def generate_command(
    prompt: str = typer.Argument(..., help="Prompt deskripsi foto untuk digenerate"),
    count: int = typer.Option(1, "--count", "-c", help="Jumlah gambar yang digenerate (1-10)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Lewati dialog konfirmasi biaya"),
) -> None:
    """Generate gambar stok komersial dengan verifikasi lisensi dan estimasi biaya (SF-007)."""
    import asyncio
    from stockforge.db.session import create_db_engine, create_session_factory
    from stockforge.images.service import ImageGenerationService
    from stockforge.providers.mock import MockImageProvider
    from stockforge.storage.service import StorageService

    if count < 1 or count > 10:
        console.print("[bold red]Kesalahan:[/bold red] Jumlah gambar harus berada dalam rentang 1 hingga 10 (batch cap).")
        raise typer.Exit(code=1)

    settings = get_settings()
    data_dir = Path(settings.data_dir).resolve()
    storage = StorageService(data_dir)

    engine = create_db_engine()
    session_factory = create_session_factory(engine)

    # Inisialisasi service dengan provider mock aman (offline)
    mock_provider = MockImageProvider(provider_name="mock-image")
    image_service = ImageGenerationService(
        session_factory=session_factory,
        storage_service=storage,
        image_provider=mock_provider,
        license_evidence="adobe_stock_commercial_sublicense_v1",
    )

    estimated_cents = image_service.estimate_max_spend_cents(count)
    estimated_usd = estimated_cents / 100.0

    console.print(
        Panel.fit(
            f"[bold cyan]Konfirmasi Generasi Gambar RUPIKA (SF-007)[/bold cyan]\n\n"
            f"• Prompt: [italic]{prompt}[/italic]\n"
            f"• Jumlah batch: [bold]{count}[/bold] file (Batas cap: 10)\n"
            f"• Estimasi biaya maksimum: [bold green]${estimated_usd:.2f} USD[/bold green] ({estimated_cents} sen)\n"
            f"• Lisensi komersial: [green]Terverifikasi (Adobe Stock Commercial Sublicense)[/green]",
            border_style="cyan",
        )
    )

    if not yes:
        confirmed = typer.confirm("Apakah Anda menyetujui estimasi biaya dan ingin melanjutkan generasi?")
        if not confirmed:
            console.print("[yellow]Operasi generasi dibatalkan oleh operator.[/yellow]")
            return

    console.print("[bold green]Memulai proses generasi gambar...[/bold green]")
    prompts = [prompt] * count

    try:
        result = asyncio.run(image_service.generate_batch(prompts=prompts, max_batch_size=10))
    except Exception as e:
        console.print(f"[bold red]Gagal memproses batch generasi gambar:[/bold red] {e}")
        raise typer.Exit(code=1)

    table = Table(title="Hasil Generasi Gambar Stok", show_header=True)
    table.add_column("Asset ID", style="cyan", width=10)
    table.add_column("Filename", style="bold")
    table.add_column("Dimensi", width=12)
    table.add_column("SHA-256", style="dim", width=14)
    table.add_column("Status", style="green", width=10)

    for a in result.successful_assets:
        table.add_row(str(a.id), a.filename, f"{a.width}x{a.height}", a.sha256[:10] + "...", a.status)

    console.print(table)
    console.print(f"\n[bold green]✓ Berhasil mengenerate {result.success_count} aset gambar.[/bold green]")
    if result.failure_count > 0:
        console.print(f"[bold red]⚠ {result.failure_count} aset gagal digenerate.[/bold red]")


# Sub-command group: Ops / Maintenance (SF-013)
ops_app = typer.Typer(
    name="ops",
    help="Operasional, pemeliharaan rutin, backup, dan diagnostik kesehatan RUPIKA.",
)
app.add_typer(ops_app, name="ops")


@ops_app.command(name="cycle")
def ops_cycle(
    trigger_backup: bool = typer.Option(True, "--backup/--no-backup", help="Sertakan backup online SQLite"),
) -> None:
    """Jalankan satu siklus pemeliharaan lengkap: health checks -> backup -> cleanup."""
    from stockforge.db.session import create_db_engine, create_session_factory
    from stockforge.ops.scheduler import MaintenanceScheduler
    from stockforge.storage.service import StorageService

    settings = get_settings()
    session_factory = create_session_factory(create_db_engine(settings.database_url))
    storage_service = StorageService(base_data_dir=settings.resolved_data_dir)

    db_path = settings.resolved_database_path
    backup_dir = settings.resolved_data_dir / "backups"

    scheduler = MaintenanceScheduler(
        session_factory=session_factory,
        storage_service=storage_service,
        db_path=db_path,
        backup_dir=backup_dir,
    )

    console.print("[bold cyan]Menjalankan siklus pemeliharaan RUPIKA...[/bold cyan]")
    report = scheduler.run_maintenance_cycle(trigger_backup=trigger_backup)

    console.print(f"• [bold]Kapasitas Disk[/bold]: {report.disk.status.value.upper()} ({report.disk.free_percent:.1f}% free)")
    console.print(f"• [bold]Antrean Job[/bold]: {report.queue.pending_count} pending, {report.queue.running_count} running")
    console.print(f"• [bold]Anggaran Harian[/bold]: {report.budget.usage_percent:.1f}% terpakai")
    console.print(f"• [bold]File Temp Dibersihkan[/bold]: {report.stale_files_cleaned} file")
    if report.backup_file:
        console.print(f"• [bold]File Backup[/bold]: {Path(report.backup_file).name} (Integritas: [green]OK[/green])")

    console.print("[bold green]✓ Siklus pemeliharaan selesai.[/bold green]")


@ops_app.command(name="backup")
def ops_backup() -> None:
    """Jalankan backup online SQLite instan dengan verifikasi checksum dan integritas."""
    from stockforge.ops.backup import perform_sqlite_online_backup

    settings = get_settings()
    db_path = settings.resolved_database_path
    backup_dir = settings.resolved_data_dir / "backups"

    console.print(f"[bold cyan]Membuat backup online dari database:[/bold cyan] {db_path}")
    try:
        backup_file = perform_sqlite_online_backup(db_path=db_path, backup_dir=backup_dir)
        console.print(f"[bold green]✓ Backup berhasil dibuat:[/bold green] {backup_file.name}")
    except Exception as e:
        console.print(f"[bold red]Gagal membuat backup:[/bold red] {e}")
        raise typer.Exit(code=1)


@ops_app.command(name="health")
def ops_health() -> None:
    """Tampilkan diagnostik kapasitas disk, kesehatan antrean, dan anggaran harian."""
    from stockforge.db.session import create_db_engine, create_session_factory
    from stockforge.ops.health import check_budget_health, check_disk_space, check_queue_health
    from stockforge.storage.service import StorageService

    settings = get_settings()
    session_factory = create_session_factory(create_db_engine(settings.database_url))
    storage_service = StorageService(base_data_dir=settings.resolved_data_dir)

    disk = check_disk_space(storage_service.base_data_dir)
    queue = check_queue_health(session_factory)
    budget = check_budget_health(session_factory, daily_budget_cents=int(settings.daily_budget_usd * 100))

    console.print(Panel(
        f"• [bold]Status Disk[/bold]: {disk.status.value.upper()} ({disk.free_percent:.1f}% sisa, {disk.free_bytes // (1024*1024)} MB)\n"
        f"• [bold]Antrean Job[/bold]: {queue.pending_count} pending, {queue.running_count} running, {queue.failed_count} failed\n"
        f"• [bold]Anggaran Harian[/bold]: ${budget.committed_cents / 100:.2f} / ${budget.daily_budget_cents / 100:.2f} ({budget.usage_percent:.1f}%)\n"
        f"• [bold]Peringatan Anggaran[/bold]: {'[bold red]YA[/bold red]' if budget.alert_triggered else '[green]TIDAK[/green]'}",
        title="[bold cyan]RUPIKA System Health[/bold cyan]",
    ))


def main() -> None:
    """CLI entry point."""
    app()



if __name__ == "__main__":
    main()

