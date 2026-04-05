import time
import re
import os
import sqlite3
import warnings
import pandas as pd
import chrome_version
from dotenv import load_dotenv
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

warnings.simplefilter(action="ignore")
load_dotenv()

realtrack_user = os.getenv("REALTRACK_USERNAME")
realtrack_pass = os.getenv("REALTRACK_PASSWORD")


def launch_chrome_driver(proxy_server=None, headless=False):
    browser_version = chrome_version.get_chrome_version()
    browser_version = int(browser_version.split(".")[0])
    counter_selenium = 0
    while counter_selenium < 3 and not ('driver' in locals()):
        try:
            options = uc.ChromeOptions()
            if proxy_server:
                options.add_argument(f'--proxy-server={proxy_server}')
            if headless:
                options.add_argument('--headless=new')
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-gpu')
            options.add_argument("--start-maximized")
            options.add_argument('--hide-crash-restore-bubble')
            driver = uc.Chrome(options=options, version_main=browser_version)
            break
        except Exception as e:
            print(e)
        finally:
            counter_selenium += 1
    try:
        driver.maximize_window()
    except Exception:
        pass  # --start-maximized handles this; can fail in headless
    driver.get("https://google.com/ncr")
    return driver


def wait_for_page_load(driver, timeout=30):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        return True
    except:
        return False


def is_signed_in(driver):
    try:
        WebDriverWait(driver, 1).until(
            EC.presence_of_element_located((By.XPATH, '//div[@id="headerNav"]//a[@href="?page=signout"]'))
        )
        return True
    except:
        return False


def signin_and_load_search_page(driver):
    if not is_signed_in(driver):
        driver.get('https://realtrack.com/?page=login')
        wait_for_page_load(driver, 15)
        try:
            input_login_ele = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.XPATH, '//input[@name="username"]'))
            )
        except:
            input_login_ele = None
        try:
            input_pass_ele = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.XPATH, '//input[@name="password"]'))
            )
        except:
            input_pass_ele = None
        try:
            submit_button_ele = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((By.XPATH, '//input[@value="login"]'))
            )
        except:
            submit_button_ele = None
        if input_login_ele and input_pass_ele and submit_button_ele:
            input_login_ele.clear()
            input_login_ele.send_keys(realtrack_user)
            time.sleep(1)
            input_pass_ele.clear()
            input_pass_ele.send_keys(realtrack_pass)
            time.sleep(1)
            submit_button_ele.click()
            time.sleep(1)
            wait_for_page_load(driver, 5)
            driver.get('https://realtrack.com/?page=search')
            wait_for_page_load(driver, 15)
            return True if is_signed_in(driver) else False
    else:
        driver.get('https://realtrack.com/?page=search')
        wait_for_page_load(driver, 15)
        return True


def execute_predefined_search(signed_in, driver, search_config=None):
    if not signed_in:
        return False

    from config import SEARCH_CONFIG
    cfg = search_config or SEARCH_CONFIG

    prop_type = cfg["property_type"]
    start_year = cfg["start_year"]
    min_amount = cfg["min_amount"]
    records_per_page = cfg["records_per_page"]

    type_select_xpath = '//td[contains(text(), "Type:")]//following-sibling::td//select[@name="sf3"]'
    type_select_ele = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, type_select_xpath)))
    start_year_xpath = '//td[contains(text(), "Period:")]//following-sibling::td//select[@name="startyr"]'
    start_year_ele = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, start_year_xpath)))
    min_amount_xpath = '//td[contains(text(), "$amount:")]//following-sibling::td//input[@name="minamt"]'
    min_amount_ele = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, min_amount_xpath)))
    per_page_xpath = '//td[contains(text(), "Display:")]//following-sibling::td//select[@name="sf9"]'
    per_page_ele = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, per_page_xpath)))

    dropdown = Select(type_select_ele)
    dropdown.select_by_visible_text(prop_type)
    time.sleep(1)
    dropdown = Select(start_year_ele)
    dropdown.select_by_visible_text(start_year)
    time.sleep(1)
    min_amount_ele.clear()
    min_amount_ele.send_keys(min_amount)
    time.sleep(1)
    dropdown = Select(per_page_ele)
    dropdown.select_by_visible_text(records_per_page)
    time.sleep(1)
    search_button = WebDriverWait(driver, 5).until(
        EC.element_to_be_clickable((By.XPATH, '//input[@value="search"]'))
    )
    search_button.click()
    time.sleep(1)
    return True


def should_include_line(text):
    street_keywords = {
        'rd', 'road', 'st', 'street', 'hwy', 'highway', 'ave', 'avenue',
        'blvd', 'boulevard', 'dr', 'drive', 'cres', 'crescent', 'ct', 'court',
        'pl', 'place', 'tr', 'trail', 'way', 'pky', 'parkway', 'sq', 'square',
        'nw', 'ne', 'sw', 'se'
    }
    corp_identifiers = {
        'inc', 'ltd', 'corp', 'limited', 'incorporated', 'ltee', 'ltée',
        'limitee', 'limitée', 'sarf', 'ulc', 'llp', 'gp'
    }
    words = [word.lower().strip('.,:') for word in text.split()]
    has_street = any(word in street_keywords for word in words)
    has_corp = any(word in corp_identifiers for word in words)
    if has_street and not has_corp:
        return False
    return True


def get_prop_df_from_soup(soup):
    try:
        paragraphs = soup.find_all('p')
        address_ele = soup.find('strong', id='address')
        address_siblings = address_ele.find_next_siblings(string=True)
        try: address = " & ".join([_ for _ in address_ele.stripped_strings]).strip("& ")
        except: address = ""
        try: city = [ele.text.strip() for ele in address_siblings if ' : ' in ele][0].split(' : ')[0].strip()
        except: city = ""
        try: region = [ele.text.strip() for ele in address_siblings if ' : ' in ele][0].split(' : ')[1].strip()
        except: region = ""
        try: pin = [p.text.strip().split('PIN:')[1].strip() for p in paragraphs if 'PIN:' in p.text.strip()][0]
        except: pin = ""
        try: site = "\n".join([_ for _ in [p for p in paragraphs if p.find('font') and 'Site' in p.find('font').text][0].stripped_strings]).replace('Site\n', '')
        except: site = ""
        try: instrument_num = re.search(r'ROS-\d+', soup.text).group(0)
        except: instrument_num = ""
        try: acreage = [p for p in paragraphs if re.search(r'\d+\.?\d+?\s?acre', p.text) and len(p.text.split()) == 2][0].text.strip()
        except: acreage = ""
        try: assess_roll_num = [p.find(string=True, recursive=False) for p in paragraphs if p.find('font') and 'Assessment Roll Number' in p.find('font').text][0]
        except: assess_roll_num = ""
        try: record_id = [p.find('font').text.strip().split('\xa0')[-1] for p in paragraphs if p.find('a') and ('prev' in p.find('a').text or 'next' in p.find('a').text)][0]
        except: record_id = ""

        raw_text = soup.get_text(separator="\n", strip=True)

        property_dict = {
            "address": address,
            "city": city,
            "region": region,
            "pin": pin,
            "site_description": site,
            "instrument_number": instrument_num,
            "acreage": acreage,
            "assessment_roll_number": assess_roll_num,
            "record_id": record_id,
            "raw_text": raw_text,
        }
        return pd.DataFrame([property_dict])
    except Exception as e:
        print(f"  Warning: Failed to parse property data: {e}")
        return pd.DataFrame()


def get_transaction_df_from_soup(soup):
    try:
        paragraphs = soup.find_all('p')
        address_ele = soup.find('strong', id='address')
        address_siblings = address_ele.find_next_siblings(string=True)
        try:
            sale_date_str = [ele.text.strip() for ele in address_siblings if re.search(r'\d{1,2} [A-Z][a-z]{2} \d{4}', ele)][0]
            sale_date_str = re.sub(r'[\xa0]+', '<>', sale_date_str)
            sale_date = sale_date_str.split('<>')[0].strip()
        except: sale_date = ""
        try: purchase_price = sale_date_str.split('<>')[1]
        except: purchase_price = ""
        try:
            cash_str = [p.text for p in paragraphs if "cash:" in p.text][0]
            cash = re.search(r'cash:\s?(\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', cash_str).group(1)
        except: cash = ""
        try:
            vbt_str = [p.text for p in paragraphs if "assumed/vtb" in p.text or 'debt:' in p.text][0]
            vbt_debt = re.search(r'debt:\s?(\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', vbt_str).group(1)
        except: vbt_debt = ""
        try: portfolio_flag = "Portfolio" if bool([_.text for _ in address_ele.find_next_siblings('font') if _.text == 'Portfolio']) else ""
        except: portfolio_flag = ""

        transaction_dict = {
            "sale_date": sale_date,
            "purchase_price": purchase_price,
            "cash": cash,
            "assumed_vbt_debt": vbt_debt,
            "portfolio_flag": portfolio_flag,
        }
        return pd.DataFrame([transaction_dict])
    except Exception as e:
        print(f"  Warning: Failed to parse transaction data: {e}")
        return pd.DataFrame()


def get_chargees_df_from_soup(soup):
    try:
        paragraphs = soup.find_all('p')
        chargee_list = [p for p in paragraphs if 'chargee:' in p.get_text()]
        chargees = []
        for chargee in chargee_list:
            parts = [re.sub(r'[\xa0]+', ' ', _) for _ in chargee.stripped_strings]
            try: chargee_name = [_ for _ in parts if 'chargee:' in _][0].split('chargee:')[1].strip()
            except: chargee_name = ""
            try: principal = re.search(r'\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?', [_ for _ in parts if 'principal:' in _ or 'rate:' in _][0]).group(0)
            except: principal = ""
            try: rate = re.search(r'\d+(?:\.\d+)?%', [_ for _ in parts if 'principal:' in _ or 'rate:' in _][0]).group(0)
            except: rate = ""
            date_pattern = r'\d{2}/\d{2}/\d{4}'
            try: due = re.search(r'due:\s*(' + date_pattern + ')', [_ for _ in parts if 'registered:' in _ or 'due:' in _][0]).group(1)
            except: due = ""
            try: reg = re.search(r'registered:\s*(' + date_pattern + ')', [_ for _ in parts if 'registered:' in _ or 'due:' in _][0]).group(1)
            except: reg = ""
            chargee_dict = {
                "chargee": chargee_name,
                "principal": principal,
                "rate": rate,
                "registered_date": reg,
                "due_date": due,
            }
            chargees.append(chargee_dict)
        return pd.DataFrame(chargees)
    except Exception as e:
        print(f"  Warning: Failed to parse charges data: {e}")
        return pd.DataFrame()


def get_party_role_str(parties_df, party_role):
    row = parties_df[parties_df['party_role'] == party_role].reset_index(drop=True).iloc[0]
    legal_name = f"{row['legal_name']} / {row['legal_name_2']}" if row['legal_name_2'] else row['legal_name']
    attn_care = row['attention'] if row['attention'] else row['care_of'] if row['care_of'] else ""
    if attn_care and row['phone']:
        final_str = f"{legal_name}, Attn: {attn_care}, {row['phone']}"
    elif row['phone']:
        final_str = f"{legal_name}, {row['phone']}"
    elif attn_care:
        final_str = f"{legal_name}, c/o {attn_care}"
    else:
        final_str = legal_name
    return final_str


def get_parties_df_from_soup(soup):
    try:
        roles = ['Transferor(s)', 'Transferee(s)']
        parties = []
        for role in roles:
            party_ele = soup.find('font', string=lambda x: x and role in x)
            party_txt = [ele.split('\xa0')[0].strip() for ele in party_ele.parent.stripped_strings if role not in ele]
            try: legal_name = party_txt[0]
            except: legal_name = ""
            try: legal_name_2 = party_txt[1]
            except: legal_name_2 = ""
            try: party_num = re.search(r'[0-9]{3}-[0-9]{3}-[0-9]{4}', party_ele.parent.text).group(0)
            except: party_num = ""
            try: attn = [_ for _ in party_ele.parent.find_next_sibling('p').stripped_strings if 'Attn:' in _][0].split('ttn: ')[1].strip()
            except: attn = ""
            try: care_of = [_ for _ in party_ele.parent.find_next_sibling('p').stripped_strings if 'c/o' in _][0].replace('c/o', '').strip()
            except: care_of = ""
            address_parts = [_ for _ in party_ele.parent.find_next_sibling('p').stripped_strings]
            try: street = address_parts[-3].replace('c/o', '').strip()
            except: street = ""
            try: city = address_parts[-2].split(', ')[0].strip()
            except: city = ""
            try: province = address_parts[-2].split(', ')[1].strip()
            except: province = ""
            try: postal_code = address_parts[-1]
            except: postal_code = ""
            party_dict = {
                "party_role": role.replace("(s)", ""),
                "legal_name": legal_name,
                "legal_name_2": legal_name_2,
                "phone": party_num,
                "attention": attn,
                "care_of": care_of,
                "address": street,
                "city": city,
                "province": province,
                "postal_code": postal_code,
            }
            parties.append(party_dict)
        return pd.DataFrame(parties)
    except Exception as e:
        print(f"  Warning: Failed to parse parties data: {e}")
        return pd.DataFrame()


def upsert_data(df, table_name, db_file, primary_col="record_id"):
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    try:
        columns = df.columns.tolist()
        col_defs = []
        for col in columns:
            if col == primary_col:
                col_defs.append(f'"{col}" TEXT PRIMARY KEY')
            else:
                col_defs.append(f'"{col}" TEXT')
        create_table_sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" ({", ".join(col_defs)});'
        cur.execute(create_table_sql)
        placeholders = ", ".join(["?"] * len(columns))
        col_names = ", ".join([f'"{c}"' for c in columns])
        upsert_sql = f'INSERT OR REPLACE INTO "{table_name}" ({col_names}) VALUES ({placeholders})'
        data_tuples = [tuple(str(v) if pd.notna(v) else None for v in row) for row in df.to_numpy()]
        cur.executemany(upsert_sql, data_tuples)
        conn.commit()
        print(f"  DB: {cur.rowcount} rows upserted in '{table_name}'")
    except Exception as e:
        conn.rollback()
        print(f"  DB Error: {e}")
        raise
    finally:
        conn.close()


def read_data_from_db(db_file, table_name, columns="*", addl_query=None, output="dataframe"):
    try:
        conn = sqlite3.connect(db_file)
        if isinstance(columns, list):
            columns = ", ".join(columns)
        query = f"SELECT {columns} FROM {table_name}"
        if addl_query:
            query += addl_query
        df = pd.read_sql_query(query, conn)
        if output == "dataframe":
            return df
        elif output == "json":
            return df.to_json()
    except Exception as e:
        print(f"Error reading data from database: {e}")
        return pd.DataFrame()
    finally:
        try:
            conn.close()
        except:
            pass


def get_existing_record_ids(db_file):
    """Get set of already-scraped record_ids for resume capability."""
    try:
        conn = sqlite3.connect(db_file)
        df = pd.read_sql_query('SELECT record_id FROM "Property"', conn)
        conn.close()
        return set(df['record_id'].tolist())
    except:
        return set()
