import csv
import io
import requests
from flask import Flask, render_template, request

app = Flask(__name__)

CSV_URL = "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv"

def get_data_from_sheet():
    data = {}
    try:
        response = requests.get(CSV_URL)
        response.encoding = 'utf-8'
        # استخدام القارئ الذكي الذي يبحث بالأسماء بدلاً من الأرقام
        csv_reader = csv.DictReader(io.StringIO(response.text))
        
        for row in csv_reader:
            year = term = subject = chapter = title = video_url = download_url = content_type = file_url = ""
            
            # البحث عن البيانات عبر الكلمات المفتاحية في عناوين الشيت
            for key, val in row.items():
                if not key or val is None: continue
                k = str(key).strip()
                v = str(val).strip()
                
                if "سنة" in k: year = v
                elif "ترم" in k: term = v
                elif "مادة" in k: subject = v
                elif "شبتر" in k or "فصل" in k: chapter = v
                elif "عنوان" in k or "درس" in k: title = v
                elif "رابط المقطع" in k: video_url = v
                elif "تحميل" in k: download_url = v
                elif "نوع" in k: content_type = v
                elif "رابط الملف" in k: file_url = v

            # تجاهل الأسطر غير المكتملة
            if not year or not term or not subject or not title:
                continue
            
            if not chapter:
                chapter = "عام" # إذا نسيت كتابة شبتر، سيضعه في قسم عام

            # بناء هيكلة (سنة > ترم > مادة)
            if year not in data:
                data[year] = {"name": year, "terms": {}}
            if term not in data[year]["terms"]:
                data[year]["terms"][term] = {"name": term, "subjects": {}}
            if subject not in data[year]["terms"][term]["subjects"]:
                data[year]["terms"][term]["subjects"][subject] = {
                    "id": f"{year}_{term}_{subject}".replace(" ", "_"),
                    "name": subject,
                    "types": {
                        "videos": {"chapters": {}},
                        "files": {"chapters": {}}
                    }
                }

            subj_data = data[year]["terms"][term]["subjects"][subject]

            # فرز المحتوى (فيديو أو ملف) ووضعه داخل الشبتر الصحيح
            if "فيديو" in content_type:
                final_url = video_url if video_url else download_url
                if final_url:
                    if chapter not in subj_data["types"]["videos"]["chapters"]:
                        subj_data["types"]["videos"]["chapters"][chapter] = {"name": chapter, "items": []}
                    subj_data["types"]["videos"]["chapters"][chapter]["items"].append({"title": title, "url": final_url})
            
            elif "ملف" in content_type:
                if file_url:
                    if chapter not in subj_data["types"]["files"]["chapters"]:
                        subj_data["types"]["files"]["chapters"][chapter] = {"name": chapter, "items": []}
                    subj_data["types"]["files"]["chapters"][chapter]["items"].append({"title": title, "url": file_url})

        # تحويل القواميس إلى قوائم لتسهيل عرضها
        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())
                for s in t["subjects"]:
                    s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                    s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())

    except Exception as e:
        print("Error fetching data:", e)
    
    return data

@app.route('/')
def home():
    DATA = get_data_from_sheet()
    
    year = request.args.get('year')
    term = request.args.get('term')
    subject_id = request.args.get('subject')
    content_type = request.args.get('type')
    chapter_name = request.args.get('chapter')

    # 6. عرض الروابط والمحتوى النهائي داخل الشبتر
    if subject_id and content_type and chapter_name:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        for c in s["types"][content_type]["chapters"]:
                            if c["name"] == chapter_name:
                                return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type)

    # 5. عرض الشباتر بناءً على النوع (فيديوهات أو ملفات)
    if subject_id and content_type:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        chapters = s["types"][content_type]["chapters"]
                        return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=chapters)

    # 4. اختيار نوع المحتوى (فيديو أو ملف) بعد اختيار المادة
    if subject_id:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        return render_template('index.html', view='select_type', subject=s)

    # 3. اختيار المادة
    if year and term:
        if year in DATA and term in DATA[year]["terms"]:
            return render_template('index.html', view='subjects', year_name=DATA[year]["name"], term_name=DATA[year]["terms"][term]["name"], subjects=DATA[year]["terms"][term]["subjects"], y_id=year)

    # 2. اختيار الترم
    if year:
        if year in DATA:
            return render_template('index.html', view='terms', year_name=DATA[year]["name"], terms=DATA[year]["terms"], y_id=year)

    # 1. اختيار السنة
    return render_template('index.html', view='years', data=DATA)

if __name__ == '__main__':
    app.run()
