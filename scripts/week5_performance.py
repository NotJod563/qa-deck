"""Run controlled local Week 5 performance measurements for QA Deck."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter_ns

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from qa_deck import create_app  # noqa: E402
from qa_deck.domain import (  # noqa: E402
    PluginConfiguration,
    Product,
    ProductSetupBundle,
    Snapshot,
    SnapshotResource,
)
from qa_deck.domain.snapshot import SnapshotCaptureResult  # noqa: E402
from qa_deck.plugins import PluginManager  # noqa: E402
from qa_deck.plugins.builtin.log_collector import (  # noqa: E402
    LogCollectionService,
)
from qa_deck.product_setup import ProductSetupService  # noqa: E402
from qa_deck.snapshot import SnapshotBuilder, SnapshotDiffer  # noqa: E402
from qa_deck.storage import (  # noqa: E402
    OperationLogRepository,
    PluginConfigurationRepository,
)
from qa_deck.web.routes import _product_setup_json  # noqa: E402

WARM_UP_ITERATIONS = 3
MEASURED_ITERATIONS = 20
REPORT_DIRECTORY = REPOSITORY_ROOT / "docs" / "reports" / "assets" / "week-05"
JSON_REPORT = REPORT_DIRECTORY / "performance-results.json"
CSV_REPORT = REPORT_DIRECTORY / "performance-results.csv"
DISCLAIMER = (
    "Local controlled measurements of representative QA Deck MVP operations; "
    "these are not production benchmarks."
)


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    benchmark: str
    case: str
    iterations: int
    mean_ms: float
    median_ms: float
    p95_ms: float
    min_ms: float
    max_ms: float
    notes: str


class SyntheticSnapshotProvider:
    """Deterministic provider used through the real SnapshotBuilder."""

    identifier = "synthetic-performance-provider"
    display_name = "Synthetic Performance Provider"
    description = "Controlled performance data provider."
    version = "1.0"

    def __init__(self, resources: tuple[SnapshotResource, ...]) -> None:
        self._result = SnapshotCaptureResult(resources=resources)

    def capture_snapshot(
        self,
        product: Product,
        configuration: PluginConfiguration | None,
    ) -> SnapshotCaptureResult:
        del product, configuration
        return self._result


def main() -> None:
    """Run all scenarios, write reports, and print a compact table."""
    results: list[BenchmarkResult] = []
    with tempfile.TemporaryDirectory(prefix="qa-deck-week5-performance-") as root:
        temporary_root = Path(root)
        results.extend(benchmark_web_rendering(temporary_root / "web"))
        results.extend(benchmark_snapshot_capture(temporary_root / "capture"))
        results.extend(benchmark_snapshot_diff())
        results.extend(benchmark_product_setup(temporary_root / "setup"))
        results.extend(benchmark_log_collector(temporary_root / "logs"))

    metadata = environment_metadata()
    write_reports(metadata, results)
    print_report(metadata, results)


def benchmark_web_rendering(root: Path) -> list[BenchmarkResult]:
    app, product = configured_app(root, product_count=1)
    client = app.test_client()
    scenarios = (
        (
            "Products list GET",
            lambda: require_status(client.get("/products"), 200),
            "Complete Flask test-client request and response for one Product.",
        ),
        (
            "Configured Product detail GET",
            lambda: require_status(client.get(f"/products/{product.id}"), 200),
            "Complete Flask request and response with representative plugin settings.",
        ),
    )
    return [
        measure("Web rendering", case, operation, notes)
        for case, operation, notes in scenarios
    ]


def benchmark_snapshot_capture(root: Path) -> list[BenchmarkResult]:
    root.mkdir(parents=True, exist_ok=True)
    product = Product("snapshot-product", "Synthetic Snapshot Product")
    results: list[BenchmarkResult] = []
    for count in (10, 100, 1000):
        manager = PluginManager()
        manager.register(SyntheticSnapshotProvider(snapshot_resources(count)))
        builder = SnapshotBuilder(
            manager,
            PluginConfigurationRepository(root / f"configurations-{count}.json"),
        )
        results.append(
            measure(
                "Snapshot Capture",
                f"{count} resources",
                lambda builder=builder: builder.build_snapshot(
                    product, "Controlled capture"
                ),
                "Actual SnapshotBuilder with one deterministic synthetic provider.",
            )
        )
    return results


def benchmark_snapshot_diff() -> list[BenchmarkResult]:
    differ = SnapshotDiffer()
    results: list[BenchmarkResult] = []
    for count in (10, 100, 1000):
        base, target = diff_snapshots(count)
        results.append(
            measure(
                "Snapshot Diff",
                f"approximately {count} resources",
                lambda base=base, target=target: differ.diff(base, target),
                "Actual SnapshotDiffer; unchanged, changed, added, and removed mix.",
            )
        )
    return results


def benchmark_product_setup(root: Path) -> list[BenchmarkResult]:
    app, _ = configured_app(root, product_count=50)
    products = app.extensions["product_repository"].list_all()
    service = ProductSetupService(
        app.extensions["plugin_manager"],
        app.extensions["plugin_configuration_repository"],
    )
    client = app.test_client()
    results: list[BenchmarkResult] = []

    for count in (1, 10, 50):
        selected = tuple(products[:count])

        def serialize(selected: tuple[Product, ...] = selected) -> str:
            bundle = ProductSetupBundle(
                tuple(service.export(product) for product in selected)
            )
            return _product_setup_json(bundle.to_dict())

        payload = serialize().encode("utf-8")
        results.append(
            measure(
                "Product Setup serialization",
                f"{count} Product{'s' if count != 1 else ''}",
                serialize,
                "Actual service export, bundle validation, and JSON serialization.",
            )
        )

        def parse_preview(payload: bytes = payload) -> None:
            response = client.post(
                "/product-setup/import/configure",
                data={"setup_file": (io.BytesIO(payload), "setup-bundle.json")},
            )
            require_status(response, 200)

        results.append(
            measure(
                "Product Setup parsing/preview",
                f"{count} Product{'s' if count != 1 else ''}",
                parse_preview,
                "Actual upload parsing, validation, preparation, review, and "
                "preview render; no import confirmation.",
            )
        )
    return results


def benchmark_log_collector(root: Path) -> list[BenchmarkResult]:
    root.mkdir(parents=True, exist_ok=True)
    scenarios = (
        ("100 small files", 100, 256),
        ("1000 small files", 1000, 256),
        ("approximately 10 MB total", 10, 1024 * 1024),
    )
    results: list[BenchmarkResult] = []
    for case, file_count, file_size in scenarios:
        source = root / case.replace(" ", "-")
        source.mkdir()
        content = deterministic_bytes(file_size)
        for index in range(file_count):
            (source / f"log-{index:04d}.txt").write_bytes(content)
        product = Product("log-product", "Synthetic Log Product")
        configuration = PluginConfiguration(
            product.id,
            "log-collector",
            True,
            {"log_directories": [str(source)]},
        )
        operation_number = 0

        def collect() -> object:
            nonlocal operation_number
            operation_number += 1
            service = LogCollectionService(
                max_files=file_count + 1,
                max_total_bytes=(file_count * file_size) + 1,
                max_entries=file_count + 10,
                operation_logs=OperationLogRepository(
                    root / f"operations-{case}-{operation_number}.json"
                ),
            )
            result = service.collect(product, configuration)
            if not result.has_archive or result.file_count != file_count:
                raise RuntimeError(f"Log collection failed for {case}")
            return result

        results.append(
            measure(
                "Log Collector scan/archive",
                case,
                collect,
                "Actual bounded scan and ZIP creation over "
                f"{file_count} synthetic files.",
                after_iteration=cleanup_log_collection,
            )
        )
    return results


def configured_app(root: Path, product_count: int) -> tuple[object, Product]:
    root.mkdir(parents=True, exist_ok=True)
    app = create_app(
        {
            "TESTING": True,
            "PRODUCT_DATA_PATH": root / "products.json",
            "PLUGIN_CONFIGURATION_DATA_PATH": root / "configurations.json",
            "OPERATION_LOG_DATA_PATH": root / "operations.json",
            "ENVIRONMENT_PROFILE_DATA_PATH": root / "profiles.json",
            "SNAPSHOT_DATA_PATH": root / "snapshots.json",
            "PLUGIN_BACKUP_ROOT": root / "backups",
            "PRODUCT_SETUP_MAX_BYTES": 4 * 1024 * 1024,
        }
    )
    products = app.extensions["product_repository"]
    configurations = app.extensions["plugin_configuration_repository"]
    manager = app.extensions["plugin_manager"]
    first_product: Product | None = None

    for index in range(product_count):
        product_root = root / f"product-{index:03d}"
        license_root = product_root / "licenses"
        log_root = product_root / "logs"
        license_root.mkdir(parents=True)
        log_root.mkdir()
        executable = product_root / "application.exe"
        executable.write_bytes(b"synthetic executable marker")
        (license_root / "license.dat").write_bytes(b"synthetic license marker")
        (log_root / "application.log").write_bytes(b"synthetic log marker")
        product = Product(
            f"performance-product-{index:03d}",
            f"Performance Product {index + 1:03d}",
            description="Controlled synthetic Product for local measurements.",
            executable_path=str(executable),
            working_directory=str(product_root),
            launch_arguments=["--controlled", str(index)],
        )
        products.add(product)
        first_product = first_product or product
        configurations.upsert(
            manager.get("license-manager").create_configuration(
                product_id=product.id,
                enabled=True,
                license_directory=str(license_root),
                license_files_text="license.dat",
            )
        )
        configurations.upsert(
            manager.get("log-collector").create_configuration(
                product.id,
                True,
                [str(log_root)],
            )
        )
        configurations.upsert(
            manager.get("windows-registry").create_configuration(
                product_id=product.id,
                enabled=False,
                value_targets_json="[]",
                branch_targets_json="[]",
                presets_json="[]",
            )
        )

    if first_product is None:
        raise RuntimeError("At least one benchmark Product is required")
    return app, first_product


def snapshot_resources(count: int) -> tuple[SnapshotResource, ...]:
    return tuple(
        SnapshotResource(
            source="synthetic-performance-provider",
            resource_type="controlled-resource",
            identifier=f"resource-{index:04d}",
            state={
                "enabled": index % 2 == 0,
                "value": index,
                "metadata": {"group": index % 10, "label": f"item-{index:04d}"},
            },
        )
        for index in range(count)
    )


def diff_snapshots(count: int) -> tuple[Snapshot, Snapshot]:
    unchanged = count * 4 // 10
    changed = count * 3 // 10
    removed = count * 2 // 10
    common = unchanged + changed
    base_resources = list(snapshot_resources(common + removed))
    target_resources = list(base_resources[:unchanged])
    for index in range(unchanged, common):
        target_resources.append(
            SnapshotResource(
                source="synthetic-performance-provider",
                resource_type="controlled-resource",
                identifier=f"resource-{index:04d}",
                state={
                    "enabled": index % 2 != 0,
                    "value": index + 1,
                    "metadata": {"group": index % 10, "label": "changed"},
                },
            )
        )
    while len(target_resources) < count:
        index = len(base_resources) + len(target_resources)
        target_resources.append(
            SnapshotResource(
                source="synthetic-performance-provider",
                resource_type="controlled-resource",
                identifier=f"added-{index:04d}",
                state={"enabled": True, "value": index},
            )
        )
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    return (
        Snapshot("base", "snapshot-product", timestamp, "Base", tuple(base_resources)),
        Snapshot(
            "target",
            "snapshot-product",
            timestamp,
            "Target",
            tuple(target_resources),
        ),
    )


def deterministic_bytes(size: int) -> bytes:
    pattern = b"QA Deck controlled synthetic log line 0123456789\n"
    return (pattern * math.ceil(size / len(pattern)))[:size]


def measure(
    benchmark: str,
    case: str,
    operation: Callable[[], object],
    notes: str,
    *,
    after_iteration: Callable[[object], None] | None = None,
) -> BenchmarkResult:
    for _ in range(WARM_UP_ITERATIONS):
        warm_up_result = operation()
        if after_iteration is not None:
            after_iteration(warm_up_result)
    durations_ms: list[float] = []
    for _ in range(MEASURED_ITERATIONS):
        started = perf_counter_ns()
        operation_result = operation()
        durations_ms.append((perf_counter_ns() - started) / 1_000_000)
        if after_iteration is not None:
            after_iteration(operation_result)
    ordered = sorted(durations_ms)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return BenchmarkResult(
        benchmark=benchmark,
        case=case,
        iterations=len(durations_ms),
        mean_ms=round(statistics.fmean(durations_ms), 3),
        median_ms=round(statistics.median(durations_ms), 3),
        p95_ms=round(ordered[p95_index], 3),
        min_ms=round(ordered[0], 3),
        max_ms=round(ordered[-1], 3),
        notes=notes,
    )


def cleanup_log_collection(result: object) -> None:
    temporary_directory = getattr(result, "temporary_directory", None)
    if isinstance(temporary_directory, Path):
        shutil.rmtree(temporary_directory, ignore_errors=True)


def require_status(response: object, expected: int) -> None:
    status_code = getattr(response, "status_code", None)
    if status_code != expected:
        raise RuntimeError(f"Expected HTTP {expected}, received {status_code}")
    get_data = getattr(response, "get_data", None)
    if callable(get_data):
        get_data()


def environment_metadata() -> dict[str, object]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "python_version": platform.python_version(),
        "operating_system": platform.system(),
        "machine_platform": platform.platform(),
        "logical_cpu_count": os.cpu_count(),
        "git_commit": commit,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def write_reports(
    metadata: dict[str, object], results: list[BenchmarkResult]
) -> None:
    REPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    JSON_REPORT.write_text(
        json.dumps(
            {
                "description": DISCLAIMER,
                "environment": metadata,
                "results": [asdict(result) for result in results],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    with CSV_REPORT.open("w", encoding="utf-8", newline="") as stream:
        fieldnames = list(asdict(results[0]))
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(result) for result in results)


def print_report(
    metadata: dict[str, object], results: list[BenchmarkResult]
) -> None:
    print(DISCLAIMER)
    print(
        f"Python {metadata['python_version']} | {metadata['machine_platform']} | "
        f"logical CPUs: {metadata['logical_cpu_count']}"
    )
    headers = (
        "Benchmark",
        "Case",
        "N",
        "Mean ms",
        "Median ms",
        "p95 ms",
        "Min ms",
        "Max ms",
    )
    rows = [
        (
            item.benchmark,
            item.case,
            str(item.iterations),
            f"{item.mean_ms:.3f}",
            f"{item.median_ms:.3f}",
            f"{item.p95_ms:.3f}",
            f"{item.min_ms:.3f}",
            f"{item.max_ms:.3f}",
        )
        for item in results
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    print(format_row(headers, widths))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(format_row(row, widths))
    print()
    print(
        "All tested representative operations completed within the recorded "
        "local timings."
    )
    print("Reported values describe this controlled local environment only.")


def format_row(values: tuple[str, ...], widths: list[int]) -> str:
    return " | ".join(
        value.ljust(widths[index]) for index, value in enumerate(values)
    )


if __name__ == "__main__":
    main()
