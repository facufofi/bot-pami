# -*- coding: utf-8 -*-
# pami_check.py
import os, time, csv, smtplib, traceback
from pathlib import Path
from datetime import datetime
from email.utils import formatdate
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email import encoders

from bs4 import BeautifulSoup
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ---- Config ----
LOGIN_URL = "https://efectores.pami.org.ar/pami_efectores/login.php?xgap_historial=clear"
LISTADO_URL = "https://efectores.pami.org.ar/pami_nc/OP/op_panel_listado.php"

PORTAL_USER = os.getenv("PORTAL_USER")
PORTAL_PASS = os.getenv("PORTAL_PASS")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
ALERT_TO = os.getenv("ALERT_TO")
ALERT_FROM = os.getenv("ALERT_FROM", SMTP_USER)

ESTADOS_A_BUSCAR = [
    "PENDIENTE DE ACEPTACION",
    "PENDIENTE DE ACEPTACION POR PROVEEDOR",
    "PENDIENTE DE ACEPTACION PRESTADOR",
    "PENDIENTE DE RETIRO DE EQUIPOS",
]

SALIDA_DIR = Path("salidas"); SALIDA_DIR.mkdir(exist_ok=True)

def send_email_with_optional_attachment(subject: str, html_body: str, attachment_path: Path | None):
    msg = MIMEMultipart()
    msg["From"] = ALERT_FROM
    msg["To"] = ALERT_TO
    msg["Date"] = formatdate(localtime=True)
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if attachment_path and attachment_path.exists():
        with open(attachment_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{attachment_path.name}"')
        msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(ALERT_FROM, [ALERT_TO], msg.as_string())

def _select_estado_dropdown(page):
    # 1) Preferir por label accesible
    try:
        return page.get_by_label("Estado").nth(0)
    except Exception:
        pass
    # 2) Fallback: primer <select> visible
    return page.locator("select").first

def login_and_open_list(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    # Campos comunes (ajustables si hiciera falta)
    page.fill('input[type="text"]', PORTAL_USER, timeout=10000)
    page.fill('input[type="password"]', PORTAL_PASS, timeout=10000)
    try:
        page.click('button[type="submit"]', timeout=5000)
    except PwTimeout:
        page.get_by_role("button", name="Ingresar").click()
    page.wait_for_load_state("networkidle")
    page.goto(LISTADO_URL, wait_until="networkidle")

def set_estado_and_search(page, estado_label: str):
    sel = _select_estado_dropdown(page)
    sel.select_option(label=estado_label)
    page.get_by_role("button", name="Buscar", exact=True).click()
    page.wait_for_load_state("networkidle")
    time.sleep(0.8)  # settle

def extract_table_rows(page):
    html = page.content()
    soup = BeautifulSoup(html, "lxml")
    tablas = soup.find_all("table")
    if not tablas:
        return []

    candidata = None
    for t in tablas:
        thead = t.find("thead")
        tbody = t.find("tbody")
        if thead and tbody and tbody.find_all("tr"):
            candidata = t
    if not candidata:
        candidata = tablas[-1]

    headers = [th.get_text(strip=True) for th in candidata.select("thead th")]
    if not headers:
        headers = [td.get_text(strip=True) for td in candidata.select("thead td")]

    rows = []
    for tr in candidata.select("tbody tr"):
        celdas = [td.get_text(strip=True) for td in tr.find_all("td")]
        if not celdas:
            continue
        if headers and len(celdas) == len(headers):
            rows.append(dict(zip(headers, celdas)))
        else:
            rows.append({"cols": celdas})
    return rows

def main():
    if not all([PORTAL_USER, PORTAL_PASS, SMTP_USER, SMTP_PASS, ALERT_TO]):
        raise RuntimeError("Faltan variables de entorno obligatorias.")

    resumen = []
    all_rows = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            login_and_open_list(page)

            for estado in ESTADOS_A_BUSCAR:
                set_estado_and_search(page, estado)
                filas = extract_table_rows(page)
                for r in filas:
                    r["_ESTADO_FILTRADO"] = estado
                resumen.append((estado, len(filas)))
                all_rows.extend(filas)

            context.close()
            browser.close()

        total = sum(c for _, c in resumen)

        # Si no hay filas, no mandamos nada (comportamiento pedido)
        if total == 0:
            print("Sin novedades. No se envía correo.")
            return

        # CSV
        fecha = datetime.now().strftime("%Y%m%d_%H%M")
        csv_path = SALIDA_DIR / f"pami_{fecha}.csv"
        try:
            if isinstance(all_rows[0], dict) and "cols" not in all_rows[0]:
                df = pd.DataFrame(all_rows)
            else:
                df = pd.DataFrame([r.get("cols", []) for r in all_rows])
            df.to_csv(csv_path, index=False, quoting=csv.QUOTE_ALL)
        except Exception as e:
            csv_path = None
            print(f"Error guardando CSV: {e}")

        # Email
        resumen_html = "<ul>" + "".join(
            f"<li><b>{est}:</b> {cant}</li>" for est, cant in resumen
        ) + f"</ul><p><b>Total:</b> {total}</p>"

        subject = f"[Oxy Net] Novedades PAMI - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        body = f"""
        <h3>Se detectaron solicitudes en el portal PAMI</h3>
        {resumen_html}
        <p>Se adjunta CSV con el detalle si corresponde.</p>
        <p style="font-size:12px;color:#666">Bot Playwright (GitHub Actions)</p>
        """
        send_email_with_optional_attachment(subject, body, csv_path)
        print("Correo enviado.")

    except Exception:
        traza = traceback.format_exc()
        try:
            send_email_with_optional_attachment(
                "[Oxy Net] ERROR bot PAMI",
                f"<pre>{traza}</pre>",
                None
            )
        except Exception:
            pass
        raise

if __name__ == "__main__":
    main()

