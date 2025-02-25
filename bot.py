import os
import fitz
from telegram import Update
from telegram.ext import Updater, MessageHandler, Filters, CallbackContext
import requests
import tempfile
import sqlite3
from threading import Thread
import time
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
from dotenv import load_dotenv
import zipfile
import json

load_dotenv()

# إعدادات الترجمة (حسابين بس)
API_KEYS = [
    os.getenv("DEEPL_API_KEY_1"),
    os.getenv("DEEPL_API_KEY_2")
]
FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "NotoSansArabic-Regular.ttf")
conn = sqlite3.connect('translations.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS translations (original TEXT, translated TEXT)''')

def translate_text(text, api_key, retries=3):
    cached = c.execute("SELECT translated FROM translations WHERE original=?", (text,)).fetchone()
    if cached:
        return cached[0]
    
    url = "https://api-free.deepl.com/v2/translate"
    params = {
        "auth_key": api_key,
        "text": text,
        "target_lang": "AR"
    }
    for attempt in range(retries):
        try:
            response = requests.post(url, data=params)
            if response.status_code == 200:
                translated = response.json()["translations"][0]["text"]
                c.execute("INSERT INTO translations VALUES (?, ?)", (text, translated))
                conn.commit()
                return translated
            elif response.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            else:
                raise Exception(f"خطأ في الترجمة: {response.text}")
        except Exception as e:
            if attempt == retries - 1:
                raise Exception(f"فشل الترجمة بعد {retries} محاولات: {str(e)}")
    return None

def split_text_into_chunks(text, max_chars=1000):
    chunks = []
    current_chunk = ""
    for line in text.split("\n"):
        if len(current_chunk) + len(line) <= max_chars:
            current_chunk += line + "\n"
        else:
            chunks.append(current_chunk.strip())
            current_chunk = line + "\n"
    if current_chunk:
        chunks.append(current_chunk.strip())
    return chunks

def check_daily_limit(api_key, retries=3):
    url = "https://api-free.deepl.com/v2/usage"
    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}
    for attempt in range(retries):
        try:
            response = requests.get(url, headers=headers)
            if response.status_code == 200:
                usage = response.json()
                return usage["character_count"], usage["character_limit"]
            elif response.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            else:
                raise Exception("خطأ في التحقق من الاستخدام اليومي.")
        except Exception as e:
            if attempt == retries - 1:
                raise Exception(f"فشل التحقق من الحد اليومي بعد {retries} محاولات: {str(e)}")
    return None, None

def translate_chunks(chunks, api_keys):
    translated_chunks = [None] * len(chunks)
    total_chunks = len(chunks)
    completed_chunks = 0

    def translate_worker(api_key, chunks_subset, start_idx):
        nonlocal completed_chunks
        for idx, chunk in enumerate(chunks_subset):
            try:
                current_usage, daily_limit = check_daily_limit(api_key)
                if current_usage is None or current_usage >= daily_limit:
                    raise Exception(f"الحساب {api_key} تجاوز الحد الشهري.")
                translated = translate_text(chunk, api_key)
                translated_chunks[start_idx + idx] = translated
                completed_chunks += 1
                print(f"تمت ترجمة {completed_chunks}/{total_chunks} ({completed_chunks / total_chunks * 100:.2f}%)")
            except Exception as e:
                print(f"خطأ في الترجمة للحساب {api_key}: {str(e)}")

    threads = []
    chunk_size_per_account = len(chunks) // len(api_keys)
    for i, api_key in enumerate(api_keys):
        start_index = i * chunk_size_per_account
        end_index = (i + 1) * chunk_size_per_account if i < len(api_keys) - 1 else len(chunks)
        thread = Thread(target=translate_worker, args=(api_key, chunks[start_index:end_index], start_index))
        threads.append(thread)
        thread.start()

    for thread in threads:
        thread.join()

    if None in translated_chunks:
        raise Exception("فشل في ترجمة بعض الأجزاء.")
    return "\n".join(translated_chunks)

def analyze_structure(pdf_path):
    doc = fitz.open(pdf_path)
    structure = {"pages": []}
    for page in doc:
        blocks = page.get_text("dict")["blocks"]
        page_data = {"title": None, "paragraphs": [], "tables": []}
        for block in blocks:
            if "lines" in block:
                for line in block["lines"]:
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if not text:
                            continue
                        position = {"x": span["origin"][0], "y": span["origin"][1], "size": span["size"]}
                        if span["size"] > 12:
                            page_data["title"] = {"text": text, "position": position}
                        else:
                            page_data["paragraphs"].append({"text": text, "position": position})
        structure["pages"].append(page_data)
    return structure

def save_structure(structure, output_file):
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(structure, f, ensure_ascii=False, indent=2)

def rebuild_pdf(structure, translated_texts, output_path):
    new_doc = fitz.open()
    text_index = 0

    for page_data in structure["pages"]:
        page = new_doc.new_page(width=595, height=842)
        if page_data["title"]:
            pos = page_data["title"]["position"]
            page.insert_text(
                (pos["x"], pos["y"]),
                translated_texts[text_index] if text_index < len(translated_texts) else page_data["title"]["text"],
                fontsize=pos["size"],
                fontname="Noto-Sans-Arabic",
                fontfile=FONT_PATH
            )
            text_index += 1
        
        for para in page_data["paragraphs"]:
            pos = para["position"]
            if text_index < len(translated_texts):
                page.insert_text(
                    (pos["x"], pos["y"]),
                    translated_texts[text_index],
                    fontsize=pos["size"],
                    fontname="Noto-Sans-Arabic",
                    fontfile=FONT_PATH
                )
                text_index += 1

    new_doc.save(output_path)

def process_pdf(input_path, output_path):
    structure = analyze_structure(input_path)
    save_structure(structure, "structure.json")

    all_texts = []
    for page_data in structure["pages"]:
        if page_data["title"]:
            all_texts.append(page_data["title"]["text"])
        for para in page_data["paragraphs"]:
            all_texts.append(para["text"])
    
    full_text = "\n".join(all_texts)
    chunks = split_text_into_chunks(full_text, max_chars=1000)
    translated_text = translate_chunks(chunks, API_KEYS)
    translated_texts = translated_text.split("\n")

    if not os.path.exists(FONT_PATH):
        raise FileNotFoundError(f"ملف الخط غير موجود: {FONT_PATH}")
    rebuild_pdf(structure, translated_texts, output_path)

def upload_to_google_drive(file_path):
    gauth = GoogleAuth()
    import os
gauth.credentials = GoogleAuth.load_credentials_from_json(os.getenv("GOOGLE_CREDENTIALS"))
if gauth.credentials is None:
   import os
gauth.credentials = GoogleAuth.load_credentials_from_json(os.getenv("GOOGLE_CREDENTIALS"))
if gauth.credentials is None:
    gauth.ServiceAuth()
    drive = GoogleDrive(gauth)

    folder_name = "Translated Books"
    folder_list = drive.ListFile({'q': f"title='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"}).GetList()
    if folder_list:
        folder_id = folder_list[0]['id']
    else:
        folder = drive.CreateFile({'title': folder_name, 'mimeType': 'application/vnd.google-apps.folder'})
        folder.Upload()
        folder_id = folder['id']

    compressed_path = file_path + ".zip"
    with zipfile.ZipFile(compressed_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(file_path, os.path.basename(file_path))
    upload_file_path = compressed_path if os.path.getsize(file_path) > 10 * 1024 * 1024 else file_path

    file = drive.CreateFile({'title': os.path.basename(upload_file_path), 'parents': [{'id': folder_id}]})
    file.SetContentFile(upload_file_path)
    file.Upload()
    file.InsertPermission({'type': 'anyone', 'value': 'anyone', 'role': 'reader'})
    link = file['webContentLink']
    if os.path.exists(compressed_path):
        os.remove(compressed_path)
    return link

def handle_document(update: Update, context: CallbackContext):
    user = update.message.from_user
    file = update.message.document.get_file()
    update.message.reply_text("تم استلام الملف! جاري بدء الترجمة...")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as input_tmp:
        file.download_to_drive(input_tmp.name)
        output_path = f"translated_{user.id}.pdf"
        start_time = time.time()

        def translation_task():
            try:
                doc = fitz.open(input_tmp.name)
                total_pages = doc.page_count
                completed_pages = 0

                def update_progress():
                    nonlocal completed_pages
                    page_times = []
                    while completed_pages < total_pages:
                        if page_times:
                            avg_time_per_page = sum(page_times) / len(page_times)
                            remaining_time = avg_time_per_page * (total_pages - completed_pages) / 3600
                            progress = (completed_pages / total_pages) * 100
                            update.message.reply_text(
                                f"الترجمة قيد التنفيذ... {progress:.2f}% منجزة. الوقت المتبقي: {remaining_time:.1f} ساعات."
                            )
                        time.sleep(300)

                progress_thread = Thread(target=update_progress)
                progress_thread.start()

                process_pdf(input_tmp.name, output_path)
                completed_pages = total_pages
                page_times.append(time.time() - start_time)
                progress_thread.join()

                file_url = upload_to_google_drive(output_path)
                elapsed_time = (time.time() - start_time) / 3600
                stats = f"تمت الترجمة بنجاح!\nعدد الصفحات: {total_pages}\nالوقت المستغرق: {elapsed_time:.1f} ساعات"
                context.bot.send_message(chat_id=user.id, text=f"{stats}\nإليك الرابط: {file_url}")
            except Exception as e:
                update.message.reply_text(f"حدث خطأ أثناء الترجمة: {str(e)}")
            finally:
                if os.path.exists(input_tmp.name):
                    os.remove(input_tmp.name)
                if os.path.exists(output_path):
                    os.remove(output_path)

        Thread(target=translation_task).start()

TOKEN = os.getenv("TELEGRAM_TOKEN")
updater = Updater(TOKEN)
updater.dispatcher.add_handler(MessageHandler(Filters.document, handle_document))
updater.start_polling()

def cleanup_temp_files():
    while True:
        temp_dir = tempfile.gettempdir()
        for file in os.listdir(temp_dir):
            if file.startswith("tmp") and file.endswith(".pdf"):
                file_path = os.path.join(temp_dir, file)
                if os.path.getmtime(file_path) < time.time() - 24 * 3600:
                    os.remove(file_path)
        time.sleep(3600)

Thread(target=cleanup_temp_files, daemon=True).start()
