#!/usr/bin/env bash
# Clean start: remove the library database and every registered roll/output
# folder, so the next run behaves as if ScannyBoy had never been used.
#
# What gets removed:
#   1. Registered roll folders (from the `rolls` table in the library DB),
#      including any `*.scanny-staging` crash-recovery dirs inside them.
#   2. The library database itself: `~/Library/Application Support/ScannyBoy/`
#      (or the path in `SCANNY_BOY_LIBRARY_DB`, plus its `-wal`/`-shm` files).
#
# Roll folders are deleted in full. If you keep irreplaceable source scans
# (NEFs) inside a roll folder, move them out first — the script lists every
# folder it is about to delete and asks for confirmation unless `-y` is given.
#
# Usage: scripts/clean-start.sh [-y]

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ASSUME_YES=false
if [[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]]; then
	ASSUME_YES=true
fi

DB_PATH="${SCANNY_BOY_LIBRARY_DB:-$HOME/Library/Application Support/ScannyBoy/library.db}"

# The override may point at a differently named file; -wal/-shm always sit
# beside it with the same stem.
DB_SIDECARS=("${DB_PATH}-wal" "${DB_PATH}-shm")

echo "ScannyBoy clean start"
echo "  Database:      $DB_PATH"
echo

# --- 1. Registered roll/output folders -------------------------------------

ROLL_FOLDERS=()
if [[ -f "$DB_PATH" ]] && command -v sqlite3 >/dev/null 2>&1; then
	while IFS= read -r folder; do
		ROLL_FOLDERS+=("$folder")
	done < <(sqlite3 "$DB_PATH" 'SELECT folder_path FROM rolls ORDER BY folder_path;' 2>/dev/null || true)
fi

STAGING_DIRS=()
if [[ ${#ROLL_FOLDERS[@]} -gt 0 ]]; then
	for folder in "${ROLL_FOLDERS[@]}"; do
		[[ -d "$folder" ]] || continue
		while IFS= read -r -d '' staging; do
			STAGING_DIRS+=("$staging")
		done < <(find "$folder" -type d -name '*.scanny-staging' -print0 2>/dev/null)
	done
fi

if [[ ${#ROLL_FOLDERS[@]} -eq 0 ]]; then
	echo "No registered roll folders found."
else
	echo "Registered roll folders:"
	for folder in "${ROLL_FOLDERS[@]}"; do
		if [[ -d "$folder" ]]; then
			echo "  $folder"
		else
			echo "  $folder (missing, skipping)"
		fi
	done
	if [[ ${#STAGING_DIRS[@]} -gt 0 ]]; then
		echo "Staging dirs found: ${#STAGING_DIRS[@]}"
	fi
	echo
	if [[ "$ASSUME_YES" == false ]]; then
		read -r -p "Delete these folders and everything in them? [y/N] " answer
		if [[ ! "$answer" =~ ^[Yy]$ ]]; then
			echo "Aborted. Roll folders and database left untouched."
			exit 1
		fi
	fi
	for folder in "${ROLL_FOLDERS[@]}"; do
		if [[ -d "$folder" ]]; then
			rm -rf "$folder"
			echo "Removed roll folder: $folder"
		fi
	done
fi

# --- 2. Library database -----------------------------------------------------

if [[ -f "$DB_PATH" ]]; then
	rm -f "$DB_PATH" "${DB_SIDECARS[@]}"
	echo "Removed database: $DB_PATH"
else
	echo "No database found at $DB_PATH."
fi

echo
echo "Clean start complete. Run scripts/bootstrap.sh if the dev environment also needs rebuilding."
