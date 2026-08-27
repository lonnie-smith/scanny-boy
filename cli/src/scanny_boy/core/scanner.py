from dataclasses import dataclass


@dataclass
class ScanResult:
    path: str
    ok: bool


def scan(path: str) -> ScanResult:
    return ScanResult(path=path, ok=True)
