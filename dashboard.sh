#!/usr/bin/env bash
# ============================================================================
#  RealTrack Prospecting Dashboard
#  Interactive CLI for viewing stats and launching pipeline runs
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_FILE="$SCRIPT_DIR/output/RealTrack.db"
LOG_DIR="$SCRIPT_DIR/output/logs"
CONFIG_FILE="$SCRIPT_DIR/config.py"

# --- Colors & Formatting ---
BOLD='\033[1m'
DIM='\033[2m'
CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
MAGENTA='\033[35m'
BLUE='\033[34m'
WHITE='\033[37m'
RESET='\033[0m'
LINE="${DIM}$(printf '%.0s─' {1..60})${RESET}"

# --- Helpers ---
has_db() { [[ -f "$DB_FILE" ]]; }

has_sqlite() { command -v sqlite3 &>/dev/null; }

query_db() {
    sqlite3 -separator '|' "$DB_FILE" "$1" 2>/dev/null || echo ""
}

print_header() {
    clear
    echo ""
    echo -e "  ${CYAN}${BOLD}┌──────────────────────────────────────────────┐${RESET}"
    echo -e "  ${CYAN}${BOLD}│${RESET}  ${WHITE}${BOLD}  RealTrack Prospecting Dashboard${RESET}          ${CYAN}${BOLD}│${RESET}"
    echo -e "  ${CYAN}${BOLD}│${RESET}  ${DIM}  Multi-Residential Property Pipeline${RESET}       ${CYAN}${BOLD}│${RESET}"
    echo -e "  ${CYAN}${BOLD}└──────────────────────────────────────────────┘${RESET}"
    echo ""
}

# --- Search Config ---
show_config() {
    echo -e "  ${MAGENTA}${BOLD}SEARCH CONFIG${RESET}"
    echo -e "  $LINE"

    if [[ -f "$CONFIG_FILE" ]]; then
        local prop_type min_amt start_yr
        prop_type=$(grep -oP '"property_type":\s*"\K[^"]+' "$CONFIG_FILE" 2>/dev/null || echo "?")
        min_amt=$(grep -oP '"min_amount":\s*"\K[^"]+' "$CONFIG_FILE" 2>/dev/null || echo "?")
        start_yr=$(grep -oP '"start_year":\s*"\K[^"]+' "$CONFIG_FILE" 2>/dev/null || echo "?")

        printf "  ${WHITE}%-18s${RESET} %s\n" "Property Type:" "$prop_type"
        printf "  ${WHITE}%-18s${RESET} \$%s+\n" "Min Sale Amount:" "$(printf "%'d" "$min_amt" 2>/dev/null || echo "$min_amt")"
        printf "  ${WHITE}%-18s${RESET} 19%s – present\n" "Date Range:" "$start_yr"
    else
        echo -e "  ${DIM}config.py not found${RESET}"
    fi
    echo ""
}

# --- Database Stats ---
show_db_stats() {
    echo -e "  ${GREEN}${BOLD}DATABASE${RESET}"
    echo -e "  $LINE"

    if ! has_db; then
        echo -e "  ${DIM}No database yet — run the pipeline first${RESET}"
        echo ""
        return
    fi

    if ! has_sqlite; then
        echo -e "  ${DIM}sqlite3 not found — install it for DB stats${RESET}"
        echo ""
        return
    fi

    # Record counts
    local prop_count tx_count charge_count party_count
    prop_count=$(query_db "SELECT COUNT(*) FROM Property;" || echo "0")
    tx_count=$(query_db 'SELECT COUNT(*) FROM "Transaction";' || echo "0")
    charge_count=$(query_db "SELECT COUNT(*) FROM Charges;" || echo "0")
    party_count=$(query_db "SELECT COUNT(*) FROM Parties;" || echo "0")

    echo -e "  ${BOLD}Records${RESET}"
    printf "    ${WHITE}%-16s${RESET} %s\n" "Properties:" "$(printf "%'d" "$prop_count" 2>/dev/null || echo "$prop_count")"
    printf "    ${WHITE}%-16s${RESET} %s\n" "Transactions:" "$(printf "%'d" "$tx_count" 2>/dev/null || echo "$tx_count")"
    printf "    ${WHITE}%-16s${RESET} %s\n" "Charges:" "$(printf "%'d" "$charge_count" 2>/dev/null || echo "$charge_count")"
    printf "    ${WHITE}%-16s${RESET} %s\n" "Parties:" "$(printf "%'d" "$party_count" 2>/dev/null || echo "$party_count")"
    echo ""

    # Date range from transactions
    local earliest latest
    earliest=$(query_db 'SELECT MIN(sale_date) FROM "Transaction" WHERE sale_date != "";')
    latest=$(query_db 'SELECT MAX(sale_date) FROM "Transaction" WHERE sale_date != "";')
    if [[ -n "$earliest" && -n "$latest" ]]; then
        echo -e "  ${BOLD}Sale Date Range${RESET}"
        printf "    ${WHITE}%-16s${RESET} %s\n" "Earliest:" "$earliest"
        printf "    ${WHITE}%-16s${RESET} %s\n" "Latest:" "$latest"
        echo ""
    fi

    # Top cities
    local top_cities
    top_cities=$(query_db "SELECT city || ' (' || COUNT(*) || ')' FROM Property WHERE city != '' GROUP BY city ORDER BY COUNT(*) DESC LIMIT 5;")
    if [[ -n "$top_cities" ]]; then
        echo -e "  ${BOLD}Top Cities${RESET}"
        while IFS= read -r city; do
            echo "    $city"
        done <<< "$top_cities"
        echo ""
    fi

    # Price range
    local min_price max_price avg_price
    min_price=$(query_db 'SELECT MIN(CAST(REPLACE(REPLACE(purchase_price, "$", ""), ",", "") AS REAL)) FROM "Transaction" WHERE purchase_price != "";')
    max_price=$(query_db 'SELECT MAX(CAST(REPLACE(REPLACE(purchase_price, "$", ""), ",", "") AS REAL)) FROM "Transaction" WHERE purchase_price != "";')
    avg_price=$(query_db 'SELECT CAST(AVG(CAST(REPLACE(REPLACE(purchase_price, "$", ""), ",", "") AS REAL)) AS INTEGER) FROM "Transaction" WHERE purchase_price != "";')
    if [[ -n "$min_price" && "$min_price" != "" ]]; then
        echo -e "  ${BOLD}Purchase Prices${RESET}"
        printf "    ${WHITE}%-16s${RESET} \$%s\n" "Min:" "$(printf "%'.0f" "$min_price" 2>/dev/null || echo "$min_price")"
        printf "    ${WHITE}%-16s${RESET} \$%s\n" "Max:" "$(printf "%'.0f" "$max_price" 2>/dev/null || echo "$max_price")"
        printf "    ${WHITE}%-16s${RESET} \$%s\n" "Average:" "$(printf "%'.0f" "$avg_price" 2>/dev/null || echo "$avg_price")"
        echo ""
    fi

    # DB file size and last modified
    local db_size db_mtime
    if [[ "$(uname -o 2>/dev/null)" == "Msys" || "$(uname -o 2>/dev/null)" == "Cygwin" ]]; then
        db_size=$(stat -c %s "$DB_FILE" 2>/dev/null || wc -c < "$DB_FILE")
        db_mtime=$(stat -c %y "$DB_FILE" 2>/dev/null | cut -d. -f1 || echo "unknown")
    else
        db_size=$(stat -f %z "$DB_FILE" 2>/dev/null || stat -c %s "$DB_FILE" 2>/dev/null || echo "?")
        db_mtime=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$DB_FILE" 2>/dev/null || stat -c %y "$DB_FILE" 2>/dev/null | cut -d. -f1 || echo "unknown")
    fi
    db_size_mb=$(echo "scale=1; $db_size / 1048576" | bc 2>/dev/null || echo "?")

    echo -e "  ${BOLD}Database File${RESET}"
    printf "    ${WHITE}%-16s${RESET} %s MB\n" "Size:" "$db_size_mb"
    printf "    ${WHITE}%-16s${RESET} %s\n" "Last Modified:" "$db_mtime"
    echo ""
}

# --- Pipeline Status ---
show_pipeline_status() {
    echo -e "  ${YELLOW}${BOLD}PIPELINE STATUS${RESET}"
    echo -e "  $LINE"

    # Check lock file
    if [[ -f "$SCRIPT_DIR/output/.pipeline.lock" ]]; then
        echo -e "  ${RED}${BOLD}LOCKED${RESET} — pipeline may be running (or stale lock)"
        echo ""
    fi

    # Latest log
    if [[ -d "$LOG_DIR" ]]; then
        local latest_log
        latest_log=$(ls -t "$LOG_DIR"/pipeline_*.log 2>/dev/null | head -1)
        if [[ -n "$latest_log" ]]; then
            local log_name log_status
            log_name=$(basename "$latest_log")
            if grep -q "Pipeline SUCCEEDED" "$latest_log" 2>/dev/null; then
                log_status="${GREEN}SUCCEEDED${RESET}"
            elif grep -q "Pipeline FAILED" "$latest_log" 2>/dev/null; then
                log_status="${RED}FAILED${RESET}"
            else
                log_status="${DIM}unknown${RESET}"
            fi
            printf "  ${WHITE}%-16s${RESET} %b\n" "Last Run:" "$log_status"
            printf "  ${WHITE}%-16s${RESET} %s\n" "Log:" "$log_name"

            # Extract duration if available
            local duration
            duration=$(grep -oP 'Duration: \K[^\n]+' "$latest_log" 2>/dev/null || true)
            if [[ -n "$duration" ]]; then
                printf "  ${WHITE}%-16s${RESET} %s\n" "Duration:" "$duration"
            fi
        else
            echo -e "  ${DIM}No pipeline logs found${RESET}"
        fi
    else
        echo -e "  ${DIM}No logs directory${RESET}"
    fi
    echo ""
}

# --- Interactive Menu ---
show_menu() {
    echo -e "  ${BLUE}${BOLD}ACTIONS${RESET}"
    echo -e "  $LINE"
    echo -e "  ${WHITE}1${RESET})  Run full pipeline ${DIM}(headless + resume)${RESET}"
    echo -e "  ${WHITE}2${RESET})  Run with custom search params"
    echo -e "  ${WHITE}3${RESET})  Sync only ${DIM}(re-push CSVs to Airtable)${RESET}"
    echo -e "  ${WHITE}4${RESET})  Dry run ${DIM}(preview without executing)${RESET}"
    echo -e "  ${WHITE}5${RESET})  View latest log"
    echo -e "  ${WHITE}6${RESET})  Refresh dashboard"
    echo -e "  ${WHITE}q${RESET})  Exit"
    echo ""
}

run_full_pipeline() {
    echo ""
    echo -e "  ${CYAN}Launching pipeline (headless + resume)...${RESET}"
    echo ""
    cd "$SCRIPT_DIR"
    python run_pipeline.py --headless
    echo ""
    read -rp "  Press Enter to return to dashboard..."
}

run_custom_pipeline() {
    echo ""
    echo -e "  ${CYAN}Custom Search Parameters${RESET}"
    echo -e "  ${DIM}Leave blank to use defaults from config.py${RESET}"
    echo ""

    local prop_type min_amt start_yr
    read -rp "  Property type [Multi Residential]: " prop_type
    read -rp "  Min sale amount [4000000]: " min_amt
    read -rp "  Start year (2-digit) [96]: " start_yr

    local cmd="python run_pipeline.py --headless"
    [[ -n "$prop_type" ]] && cmd="$cmd --type \"$prop_type\""
    [[ -n "$min_amt" ]]   && cmd="$cmd --min-amount $min_amt"
    [[ -n "$start_yr" ]]  && cmd="$cmd --start-year $start_yr"

    echo ""
    echo -e "  ${DIM}Command: $cmd${RESET}"
    echo ""
    read -rp "  Run this? [Y/n]: " confirm
    if [[ "${confirm,,}" != "n" ]]; then
        cd "$SCRIPT_DIR"
        eval "$cmd"
    else
        echo -e "  ${DIM}Cancelled${RESET}"
    fi
    echo ""
    read -rp "  Press Enter to return to dashboard..."
}

run_sync_only() {
    echo ""
    echo -e "  ${CYAN}Syncing existing CSVs to Airtable...${RESET}"
    echo ""
    cd "$SCRIPT_DIR"
    python run_pipeline.py --sync-only
    echo ""
    read -rp "  Press Enter to return to dashboard..."
}

run_dry_run() {
    echo ""
    cd "$SCRIPT_DIR"
    python run_pipeline.py --dry-run
    echo ""
    read -rp "  Press Enter to return to dashboard..."
}

view_latest_log() {
    echo ""
    if [[ -d "$LOG_DIR" ]]; then
        local latest_log
        latest_log=$(ls -t "$LOG_DIR"/pipeline_*.log 2>/dev/null | head -1)
        if [[ -n "$latest_log" ]]; then
            echo -e "  ${DIM}--- $latest_log ---${RESET}"
            echo ""
            cat "$latest_log"
        else
            echo -e "  ${DIM}No logs found${RESET}"
        fi
    else
        echo -e "  ${DIM}No logs directory${RESET}"
    fi
    echo ""
    read -rp "  Press Enter to return to dashboard..."
}

# --- Main Loop ---
main() {
    while true; do
        print_header
        show_config
        show_db_stats
        show_pipeline_status
        show_menu

        read -rp "  Choose [1-6/q]: " choice
        case "$choice" in
            1) run_full_pipeline ;;
            2) run_custom_pipeline ;;
            3) run_sync_only ;;
            4) run_dry_run ;;
            5) view_latest_log ;;
            6) continue ;;
            q|Q) echo ""; exit 0 ;;
            *) echo -e "  ${RED}Invalid choice${RESET}"; sleep 1 ;;
        esac
    done
}

main
