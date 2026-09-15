from __future__ import annotations

from pathlib import Path

from .receipts import ReceiptError, load_receipt


def collect_diagnostics(root: Path, adapters) -> dict[str, list[dict[str, object]]]:
    clients: list[dict[str, object]] = []
    for name, adapter in adapters.items():
        detection = adapter.detect()
        clients.append(
            {
                "client": name,
                "available": detection.available,
                "executable": str(detection.executable) if detection.executable else None,
                "detail": detection.detail,
            }
        )

    installations: list[dict[str, object]] = []
    if root.is_dir():
        for receipt_path in sorted(root.glob("versions/*/*.receipt.json")):
            try:
                receipt = load_receipt(receipt_path)
                adapter = adapters[receipt.client]
                problems = adapter.verify(receipt)
                installations.append(
                    {
                        "client": receipt.client,
                        "version": receipt.version,
                        "destination": receipt.destination,
                        "status": "error" if problems else "ok",
                        "problems": problems,
                    }
                )
            except (KeyError, ReceiptError, OSError) as error:
                installations.append(
                    {
                        "client": "unknown",
                        "version": "unknown",
                        "destination": str(receipt_path),
                        "status": "error",
                        "problems": [str(error)],
                    }
                )
    return {"clients": clients, "installations": installations}
