import re
import os
import sys
import time
import argparse
import warnings
import pandas as pd
from datetime import datetime
from bs4 import BeautifulSoup as bs

warnings.simplefilter(action="ignore")

import config
import functions
from functions import (
    launch_chrome_driver, wait_for_page_load, signin_and_load_search_page,
    execute_predefined_search, get_prop_df_from_soup, get_transaction_df_from_soup,
    get_chargees_df_from_soup, get_parties_df_from_soup, get_party_role_str,
    upsert_data, read_data_from_db, get_existing_record_ids,
)


def parse_args():
    parser = argparse.ArgumentParser(description="RealTrack property scraper")
    parser.add_argument("--headless", action="store_true", help="Run Chrome in headless mode")
    parser.add_argument("--sync", action="store_true", help="Auto-sync to Airtable after scraping")
    parser.add_argument("--resume", action="store_true", help="Resume from last scraped record")
    parser.add_argument("--type", dest="property_type", help="Override property type (e.g. 'Commercial')")
    parser.add_argument("--min-amount", help="Override minimum sale amount (e.g. '2000000')")
    parser.add_argument("--start-year", help="Override start year (e.g. '00')")
    return parser.parse_args()


def build_search_config(args):
    """Merge CLI overrides with config defaults."""
    cfg = dict(config.SEARCH_CONFIG)
    if args.property_type:
        cfg["property_type"] = args.property_type
    if args.min_amount:
        cfg["min_amount"] = args.min_amount
    if args.start_year:
        cfg["start_year"] = args.start_year
    return cfg


def run_scraper(args):
    db_file = config.DB_FILE
    output_dir = config.OUTPUT_DIR
    os.makedirs(output_dir, exist_ok=True)

    search_config = build_search_config(args)
    print(f"Search: type={search_config['property_type']}, min=${search_config['min_amount']}, year={search_config['start_year']}")

    # Launch browser and sign in
    driver = launch_chrome_driver(headless=args.headless)
    functions.driver = driver

    driver.get('https://realtrack.com/?page=search')
    wait_for_page_load(driver, 15)
    signed_in = signin_and_load_search_page(driver)
    if not signed_in:
        print("ERROR: Failed to sign in to RealTrack. Check credentials in .env")
        driver.quit()
        sys.exit(1)

    execute_predefined_search(signed_in, driver, search_config)

    # Get total results count
    driver.get('https://realtrack.com/?page=details&skip=0')
    wait_for_page_load(driver, timeout=10)
    soup = bs(driver.page_source, 'html.parser')
    results_str = [
        p.text.strip() for p in soup.find_all('p')
        if p.find('a') and ('next' in p.find('a').text or 'prev' in p.find('a').text)
    ][0]
    total_results = int(re.search(r'\d+\s?[/]\s?\d+', results_str).group(0).split('/')[1].strip())
    print(f"Total results: {total_results}")

    # Resume support: find already-scraped records
    existing_ids = get_existing_record_ids(db_file) if args.resume else set()
    if args.resume and existing_ids:
        print(f"Resume mode: {len(existing_ids)} records already in DB")

    # Main scraping loop
    failed_pages = []
    scraped_count = 0

    for skip in range(0, total_results):
        url = f"https://realtrack.com/?page=details&skip={skip}"
        print(f"\rResult: {skip + 1}/{total_results}", end="", flush=True)

        try:
            driver.get(url)
            time.sleep(1)
            if not wait_for_page_load(driver, timeout=10):
                # Retry once
                time.sleep(2)
                driver.get(url)
                if not wait_for_page_load(driver, timeout=10):
                    print(f"\n  Skipping {skip + 1}: page load timeout")
                    failed_pages.append(skip)
                    continue

            soup = bs(driver.page_source, 'html.parser')
            prop_df = get_prop_df_from_soup(soup)

            if prop_df.empty:
                print(f"\n  Skipping {skip + 1}: no property data found")
                failed_pages.append(skip)
                continue

            record_id = prop_df['record_id'].iloc[0]

            # Skip if already scraped (resume mode)
            if args.resume and record_id in existing_ids:
                continue

            upsert_data(prop_df, "Property", db_file, primary_col="record_id")

            transaction_df = get_transaction_df_from_soup(soup)
            if not transaction_df.empty:
                transaction_df["property_record_id"] = record_id
                transaction_df["record_id"] = [f"{record_id}-{i + 1}" for i in range(len(transaction_df))]
                upsert_data(transaction_df, "Transaction", db_file, primary_col="record_id")

            parties_df = get_parties_df_from_soup(soup)
            if not parties_df.empty:
                parties_df["property_record_id"] = record_id
                parties_df["record_id"] = [f"{record_id}-{i + 1}" for i in range(len(parties_df))]
                upsert_data(parties_df, "Parties", db_file, primary_col="record_id")

            chargees_df = get_chargees_df_from_soup(soup)
            if not chargees_df.empty:
                chargees_df["property_record_id"] = record_id
                chargees_df["record_id"] = [f"{record_id}-{i + 1}" for i in range(len(chargees_df))]
                upsert_data(chargees_df, "Charges", db_file, primary_col="record_id")

            scraped_count += 1

        except Exception as e:
            print(f"\n  Error on result {skip + 1}: {e}")
            failed_pages.append(skip)
            continue

    driver.quit()
    print(f"\n\nScraping complete: {scraped_count} new records scraped")
    if failed_pages:
        print(f"Failed pages ({len(failed_pages)}): {failed_pages}")

    # Export to CSV and Excel
    export_data(db_file, output_dir)

    # Auto-sync to Airtable if requested
    if args.sync:
        print("\nSyncing to Airtable...")
        try:
            from airtable_sync import sync_all
            sync_all(output_dir)
        except Exception as e:
            print(f"Airtable sync error: {e}")


def export_data(db_file, output_dir):
    """Read from DB and export CSVs + Excel."""
    print("\nExporting data...")

    done_property_df = read_data_from_db(db_file, 'Property')
    done_transaction_df = read_data_from_db(db_file, '"Transaction"')
    done_charges_df = read_data_from_db(db_file, 'Charges')
    done_parties_df = read_data_from_db(db_file, 'Parties')

    print(f"Property: {done_property_df.shape[0]}, Transaction: {done_transaction_df.shape[0]}, "
          f"Charges: {done_charges_df.shape[0]}, Parties: {done_parties_df.shape[0]}")

    done_property_df.to_csv(os.path.join(output_dir, "Property.csv"), index=False)
    done_transaction_df.to_csv(os.path.join(output_dir, "Transaction.csv"), index=False)
    done_charges_df.to_csv(os.path.join(output_dir, "Chargees.csv"), index=False)
    done_parties_df.to_csv(os.path.join(output_dir, "Parties.csv"), index=False)

    # Build consolidated Excel
    excel_rows = []
    for idx, prop_row in done_property_df.iterrows():
        try:
            transaction_row = done_transaction_df[
                done_transaction_df['record_id'].str.contains(prop_row['record_id'])
            ].iloc[0]
        except (IndexError, KeyError):
            continue

        chargee_df = done_charges_df[done_charges_df['record_id'].str.contains(prop_row['record_id'])]
        parties_df = done_parties_df[done_parties_df['record_id'].str.contains(prop_row['record_id'])]

        data_dict = {
            "address": prop_row['address'],
            "city / region": f"{prop_row['city']} / {prop_row['region']}",
            "sale_date": transaction_row['sale_date'],
            "purchase_price": transaction_row['purchase_price'],
            "unit_count": prop_row.get('unit_count', ''),
            "price_per_unit": prop_row.get('price_per_unit', ''),
            "market_median_ppu": prop_row.get('market_median_ppu', ''),
            "ppu_vs_market": prop_row.get('ppu_vs_market', ''),
            "cmhc_zone": prop_row.get('cmhc_zone', ''),
            "cash / assumed_vtb_debt": f"{transaction_row['cash']} / {transaction_row['assumed_vbt_debt']}",
        }

        try:
            data_dict["Transferor"] = get_party_role_str(parties_df, "Transferor")
        except:
            data_dict["Transferor"] = ""
        try:
            data_dict["Transferee"] = get_party_role_str(parties_df, "Transferee")
        except:
            data_dict["Transferee"] = ""

        for cidx, crow in chargee_df.reset_index(drop=True).iterrows():
            chargee_str = crow['chargee']
            if crow['principal']:
                chargee_str = f"{chargee_str}, {crow['principal']}"
            if crow['rate']:
                chargee_str = f"{chargee_str}, {crow['rate']}"
            if crow['due_date']:
                chargee_str = f"{chargee_str}, due {crow['due_date']}"
            if crow['registered_date']:
                chargee_str = f"{chargee_str} reg {crow['registered_date']}"
            data_dict[f"Charge {cidx + 1}"] = chargee_str

        data_dict['record id'] = prop_row['record_id']
        excel_rows.append(data_dict)

    excel_df = pd.DataFrame(excel_rows)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = os.path.join(output_dir, f"realtrack_export_{timestamp}.xlsx")
    excel_df.to_excel(filename, index=False)
    print(f"Exported: {filename}")


if __name__ == "__main__":
    args = parse_args()
    run_scraper(args)
