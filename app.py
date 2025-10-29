from flask import Flask, request, render_template, send_from_directory
from flask_sqlalchemy import SQLAlchemy
import os
import csv
import re
import json
import hashlib
from datetime import datetime
from dateutil import parser

app = Flask(__name__)

# --- DATABASE CONFIGURATION ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///local_database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- DIRECTORY CONFIGURATION ---
UPLOAD_FOLDER = 'uploads'
CSV_FOLDER = 'csv_files'
RECONVERTED_FOLDER = 'reconverted'

# --- NSDL/CDSL CONSTANTS ---
Bnfcry = "1207650000865934"
RAW_HEADER_PREFIX = "076500DPADM"
CONSTANT_FIELDS = {"Bnfcry": Bnfcry, "Flg": "S", "Trf": "X", "Rsn": "2", "Paymod": "2"}
RAW_ORDER_NSDL = ["Tp", "Dt", "Bnfcry", "ISIN", "Qty", "Flg", "Trf", "Clnt", "Brkr", "Rsn", "Conamt", "Paymod", "Bnkno", "Bnkname", "Brnchname", "Xferdt", "Chqrefno"]
RAW_ORDER_CDSL = ["Tp", "Dt", "Bnfcry", "CtrPty", "ISIN", "Qty", "Flg", "Trf", "Rsn", "Conamt", "Paymod", "Bnkno", "Bnkname", "Brnchname", "Xferdt", "Chqrefno"]

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(CSV_FOLDER, exist_ok=True)
os.makedirs(RECONVERTED_FOLDER, exist_ok=True)

# --- DATABASE MODEL ---
class ConversionLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    daily_id = db.Column(db.Integer, nullable=False)
    generation_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    file_hash = db.Column(db.String(64), nullable=False, unique=True)
    original_filename = db.Column(db.String(255), nullable=True)
    generated_filename = db.Column(db.String(255), nullable=False)

# --- HELPER FUNCTIONS ---
def get_next_daily_id_from_db():
    today = datetime.utcnow().date()
    last_log_today = ConversionLog.query.filter_by(generation_date=today).order_by(ConversionLog.daily_id.desc()).first()
    return (last_log_today.daily_id + 1) if last_log_today else 1

def get_log_by_hash_from_db(file_hash):
    return ConversionLog.query.filter_by(file_hash=file_hash).first()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload-csv', methods=['POST'])
def upload_csv():
    file = request.files.get('csv_file')
    if not file:
        return "No file uploaded."

    file_content = file.read()
    file.seek(0)
    current_hash = hashlib.sha256(file_content).hexdigest()

    existing_log = get_log_by_hash_from_db(current_hash)
    if existing_log:
        return (f"Duplicate file content detected. This CSV was already processed as file ID "
                f"{str(existing_log.daily_id).zfill(5)} with the name: "
                f"<a href='/download/reconverted/{existing_log.generated_filename}'>{existing_log.generated_filename}</a>")

    file_id_int = get_next_daily_id_from_db()
    file_id = str(file_id_int).zfill(5)

    csv_path = os.path.join(CSV_FOLDER, f"{file_id}.csv")
    file.save(csv_path)

    # --- CORE CONVERSION LOGIC (UNCHANGED) ---
    with open(csv_path, newline='') as f:
        reader = csv.reader(f)
        all_rows = list(reader)
    if len(all_rows) < 2: return "CSV format invalid."
    headers = [h.strip() for h in all_rows[0]]
    data_rows = all_rows[1:]
    row_count = str(len(data_rows)).zfill(6)
    row_dict_first = dict(zip(headers, data_rows[0]))
    if "Dt" not in row_dict_first or not row_dict_first["Dt"]: return "Missing Dt in first row."
    try:
        file_date = parser.parse(row_dict_first["Dt"], dayfirst=True).strftime('%d%m%Y')
    except Exception:
        return f"Invalid Dt format in first row: {row_dict_first['Dt']}"
    for row in data_rows:
        row_dict = dict(zip(headers, row))
        if "Dt" in row_dict and row_dict["Dt"]:
            try:
                dt_val = parser.parse(row_dict["Dt"], dayfirst=True).strftime('%d%m%Y')
                if dt_val != file_date: return f"Mismatch: Dt {dt_val} does not match file date {file_date}"
            except Exception:
                return f"Invalid Dt format: {row_dict['Dt']}"
    todays_date = datetime.now().strftime('%d%m%Y')
    filename = f"18{Bnfcry}.{todays_date}.{file_id}"
    reconvert_path = os.path.join(RECONVERTED_FOLDER, filename)
    raw_header = f"{RAW_HEADER_PREFIX} {row_count}{file_id}{todays_date}"
    output_lines = [raw_header]
    for row in data_rows:
        row_dict = {k: (v.strip() if isinstance(v, str) else v) for k, v in dict(zip(headers, row)).items()}
        if "Dt" in row_dict and row_dict["Dt"]:
            try: row_dict["Dt"] = parser.parse(row_dict["Dt"], dayfirst=True).strftime('%d%m%Y')
            except Exception: return f"Invalid Dt format: {row_dict.get('Dt')}"
        row_dict["Xferdt"] = row_dict.get("Dt", "")
        ctrpty = row_dict.get("CtrPty", "")
        if not ctrpty or len(ctrpty) != 16: return f"Invalid CtrPty: {ctrpty}. Must be exactly 16 characters."
        is_nsdl = ctrpty.startswith("IN")
        tp_value = "4" if is_nsdl else "5"
        if is_nsdl:
            row_dict["Brkr"], row_dict["Clnt"] = ctrpty[:8], ctrpty[8:]
            order = RAW_ORDER_NSDL
        else:
            row_dict["CtrPty"] = ctrpty; row_dict.pop("Clnt", None); row_dict.pop("Brkr", None)
            order = RAW_ORDER_CDSL
        qty = row_dict.get("Qty", "").replace(",", ""); conamt = row_dict.get("Conamt", "").replace(",", "")
        if qty != "":
            try: row_dict["Qty"] = f"{float(qty):.3f}"
            except ValueError: return f"Invalid Qty: {row_dict.get('Qty')}"
        if conamt != "":
            try: row_dict["Conamt"] = f"{float(conamt):.2f}"
            except ValueError: return f"Invalid Conamt: {row_dict.get('Conamt')}"
        chqref = (row_dict.get("Chqrefno") or "").strip()
        row_dict["Chqrefno"] = chqref.zfill(8) if chqref else "00000000"
        merged = {**CONSTANT_FIELDS, **row_dict}; merged["Tp"] = tp_value
        line = ''.join([f"<{k}>{merged.get(k, '')}</{k}>" for k in order])
        output_lines.append(line)
    output_lines.append("")
    with open(reconvert_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines))

    # --- SAVE TO DATABASE ---
    new_log = ConversionLog(
        daily_id=file_id_int,
        file_hash=current_hash,
        original_filename=file.filename,
        generated_filename=filename
    )
    db.session.add(new_log)
    db.session.commit()

    return f"Raw file created: <a href='/download/reconverted/{filename}'>{filename}</a>"

@app.route('/download/<folder>/<filename>')
def download_file(folder, filename):
    folder_path = {"csv": CSV_FOLDER, "reconverted": RECONVERTED_FOLDER}.get(folder)
    return send_from_directory(folder_path, filename, as_attachment=True)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)