#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ASSETS_DIR="$PROJECT_DIR/assets"
FORCE=0
DRY_RUN=0
declare -a SPECS=()

# ======================== USER CONFIGURATION ========================
# Put absolute server paths between the quotes. Leave unused resources empty.
MANIFEST_450K=""
MANIFEST_EPIC=""
DHS_450K=""
DHS_EPIC=""
WGBS_850K_ANNO=""
# Directory containing the author's complete, unmodified CelFEER-main checkout.
CELFEER_MAIN=""
# CpG index references consumed by celfeer/pre/batch_change.py.
CELFEER_CPG_REF_HG19=""
CELFEER_CPG_REF_HG38=""
# ====================== END USER CONFIGURATION ======================

usage() {
    cat <<'EOF'
Install fixed reference files into nextflow_V1/assets.

Usage:
  1. Edit the USER CONFIGURATION section at the top of this file.
  2. Run: bash bin/install_assets.sh [options]

Options:
  --assets-dir DIR     Override destination assets directory
  --force              Replace an existing destination file
  --dry-run            Show actions without copying
  -h, --help           Show this help

Empty configuration values are skipped. At least one path must be configured.
EOF
}

while (($#)); do
    case "$1" in
        --assets-dir)
            [[ $# -ge 2 ]] || { echo "ERROR: --assets-dir requires a directory" >&2; exit 2; }
            ASSETS_DIR="$2"; shift 2 ;;
        --assets-dir=*) ASSETS_DIR="${1#*=}"; shift ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

add_configured_asset() {
    local key="$1"
    local source_path="$2"
    [[ -z "$source_path" ]] || SPECS+=("$key=$source_path")
}

add_configured_asset manifest.450k "$MANIFEST_450K"
add_configured_asset manifest.epic "$MANIFEST_EPIC"
add_configured_asset dhs.450k "$DHS_450K"
add_configured_asset dhs.epic "$DHS_EPIC"
add_configured_asset wgbs.850k_annotation "$WGBS_850K_ANNO"
add_configured_asset celfeer.main "$CELFEER_MAIN"
add_configured_asset celfeer.cpg_ref_hg19 "$CELFEER_CPG_REF_HG19"
add_configured_asset celfeer.cpg_ref_hg38 "$CELFEER_CPG_REF_HG38"

((${#SPECS[@]} > 0)) || { echo "ERROR: no resource paths are configured in the USER CONFIGURATION section" >&2; exit 2; }

destination_for() {
    case "$1" in
        manifest.450k)      printf '%s\n' 'manifests/450k/HM450.hg38.manifest.tsv' ;;
        manifest.epic)      printf '%s\n' 'manifests/epic/EPIC.hg38.manifest.tsv' ;;
        dhs.450k)           printf '%s\n' 'dhs/450k/filtered_cg_info_450k.bed' ;;
        dhs.epic)           printf '%s\n' 'dhs/epic/filtered_cg_info_epic.bed' ;;
        wgbs.850k_annotation) printf '%s\n' 'wgbs_to_850k/850k_cg_info_nona.bed' ;;
        celfeer.main)       printf '%s\n' 'celfeer/CelFEER-main' ;;
        celfeer.cpg_ref_hg19) printf '%s\n' 'celfeer/references/hg19_cpg_ref.txt' ;;
        celfeer.cpg_ref_hg38) printf '%s\n' 'celfeer/references/hg38_cpg_ref.txt' ;;
        *) return 1 ;;
    esac
}

key_for_destination() {
    local candidate
    for candidate in \
        manifest.450k manifest.epic dhs.450k dhs.epic wgbs.850k_annotation \
        celfeer.main celfeer.cpg_ref_hg19 celfeer.cpg_ref_hg38
    do
        if [[ "$(destination_for "$candidate")" == "$1" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    if [[ "$1" == celfeer/CelFEER-main/* ]]; then
        printf '%s\n' 'celfeer.main'
        return 0
    fi
    printf '%s\n' 'unregistered'
}

declare -A SEEN=()
declare -a KEYS=() SOURCES=() RELATIVES=()
for spec in "${SPECS[@]}"; do
    [[ "$spec" == *=* ]] || { echo "ERROR: invalid asset specification: $spec" >&2; exit 2; }
    key="${spec%%=*}"
    source_path="${spec#*=}"
    relative="$(destination_for "$key")" || { echo "ERROR: unsupported asset key: $key" >&2; exit 2; }
    [[ -z "${SEEN[$key]+x}" ]] || { echo "ERROR: duplicate asset key: $key" >&2; exit 2; }
    [[ -f "$source_path" || -d "$source_path" ]] || { echo "ERROR: source does not exist: $source_path" >&2; exit 2; }
    SEEN[$key]=1
    KEYS+=("$key"); SOURCES+=("$(cd -- "$(dirname -- "$source_path")" && pwd)/$(basename -- "$source_path")"); RELATIVES+=("$relative")
done

for i in "${!KEYS[@]}"; do
    destination="$ASSETS_DIR/${RELATIVES[$i]}"
    if [[ -e "$destination" && "$FORCE" != 1 ]]; then
        echo "ERROR: destination exists (use --force to replace): $destination" >&2
        exit 3
    fi
done

if [[ "$DRY_RUN" == 1 ]]; then
    for i in "${!KEYS[@]}"; do
        printf '[dry-run] %s\n  %s -> %s\n' "${KEYS[$i]}" "${SOURCES[$i]}" "$ASSETS_DIR/${RELATIVES[$i]}"
    done
    exit 0
fi

mkdir -p -- "$ASSETS_DIR"
for i in "${!KEYS[@]}"; do
    destination="$ASSETS_DIR/${RELATIVES[$i]}"
    mkdir -p -- "$(dirname -- "$destination")"
    temporary="${destination}.tmp.$$"
    if [[ -d "${SOURCES[$i]}" ]]; then
        mkdir -p -- "$temporary"
        cp -a -- "${SOURCES[$i]}/." "$temporary/"
    else
        cp -- "${SOURCES[$i]}" "$temporary"
    fi
    mv -- "$temporary" "$destination"
    printf 'installed %-22s %s\n' "${KEYS[$i]}" "$destination"
done

manifest="$ASSETS_DIR/assets_manifest.tsv"
temporary_manifest="${manifest}.tmp.$$"
declare -A PREVIOUS_SOURCES=()
if [[ -f "$manifest" ]]; then
    while IFS=$'\t' read -r old_key old_source old_destination _; do
        [[ "$old_key" == 'key' ]] && continue
        PREVIOUS_SOURCES["$old_destination"]="$old_source"
    done < "$manifest"
fi
printf 'key\tsource\tdestination\tbytes\tsha256\n' > "$temporary_manifest"

while IFS= read -r -d '' file; do
    [[ "$file" == "$manifest" || "$file" == "$temporary_manifest" || "$file" == */README.md ]] && continue
    relative="${file#"$ASSETS_DIR"/}"
    key="$(key_for_destination "$relative")"
    source_value="${PREVIOUS_SOURCES[$relative]:-unknown}"
    for i in "${!RELATIVES[@]}"; do
        if [[ "$relative" == "${RELATIVES[$i]}" || "$relative" == "${RELATIVES[$i]}/"* ]]; then source_value="${SOURCES[$i]}"; break; fi
    done
    bytes="$(wc -c < "$file" | tr -d '[:space:]')"
    checksum="$(sha256sum "$file" | awk '{print $1}')"
    printf '%s\t%s\t%s\t%s\t%s\n' "$key" "$source_value" "$relative" "$bytes" "$checksum" >> "$temporary_manifest"
done < <(find "$ASSETS_DIR" -type f -print0 | sort -z)

mv -- "$temporary_manifest" "$manifest"
echo "Asset manifest: $manifest"
