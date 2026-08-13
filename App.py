import csv
import io
import requests
from flask import Flask, render_template, request

app = Flask(__name__)

CSV_URL = "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv"

def get_data_from_sheet():
    data = {}
    if not CSV_URL.startswith("http"):
        return data

    try:
        response = requests.get(CSV_URL)
        response.encoding = 'utf-8'
        csv_reader = csv.reader(io.StringIO(response.text))
        
        next(csv_reader, None) # تخطي صف العناوين

        for row in csv_reader:
            year = row[0].strip() if len(row) > 0 else ""
            term = row[1].strip() if len(row) > 1 else ""
            subject = row[2].strip() if len(row) > 2 else ""
            chapter = row[3].strip() if len(row) > 3 else "" # العمود الجديد (الشبتر)
            title = row[4].strip() if len(row) > 4 else ""
            video_url = row[5].strip() if len(row) > 5 else ""
            download_url = row[6].strip() if len(row) > 6 else ""
            content_type = row[7].strip() if len(row) > 7 else ""
            file_url = row[8].strip() if len(row) > 8 else ""

            # تجاهل الصفوف الفارغة أو غير المكتملة
            if not year or not term or not subject or not chapter or not title:
                continue

            # بناء الهيكلة: سنة -> ترم -> مادة -> شبتر
            if year not in data:
                data[year] = {"name": year, "terms": {}}
            
            if term not in data[year]["terms"]:
                data[year]["terms"][term] = {"name": term, "subjects": {}}
            
            if subject not in data[year]["terms"][term]["subjects"]:
                subj_id = f"{year}_{term}_{subject}".replace(" ", "_")
                data[year]["terms"][term]["subjects"][subject] = {
                    "id": subj_id,
                    "name": subject,
                    "chapters": {}
                }
                
            if chapter not in data[year]["terms"][term]["subjects"][subject]["chapters"]:
                chap_id = f"{year}_{term}_{subject}_{chapter}".replace(" ", "_")
                data[year]["terms"][term]["subjects"][subject]["chapters"][chapter] = {
                    "id": chap_id,
                    "name": chapter,
                    "videos": [],
                    "explanations": []
                }

            # الفرز داخل الشبتر
            if content_type == "فيديو":
                final_url = video_url if video_url else download_url
                if final_url:
                    data[year]["terms"][term]["subjects"][subject]["chapters"][chapter]["videos"].append({"title": title, "url": final_url})
            
            elif content_type == "ملف":
                if file_url:
                    data[year]["terms"][term]["subjects"][subject]["chapters"][chapter]["explanations"].append({"title": title, "url": file_url})

        # تحويل القواميس إلى قوائم للواجهة
        for y in data.values():
            for t in y["terms"].values():
                subj_list = []
                for s_name, s_data in t["subjects"].items():
                    s_data["chapters"] = list(s_data["chapters"].values())
                    subj_list.append(s_data)
                t["subjects"] = subj_list

    except Exception as e:
        print("Error fetching data:", e)
    
    return data

@app.route('/')
def home():
    DATA = get_data_from_sheet()
    
    year = request.args.get('year')
    term = request.args.get('term')
    subject_id = request.args.get('subject')
    chapter_id = request.args.get('chapter')
    content_type = request.args.get('type')

    # 6. عرض المحتوى (الدروس/الملفات)
    if chapter_id and content_type:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    for c in s["chapters"]:
                        if c["id"] == chapter_id:
                            return render_template('index.html', view='content', chapter=c, content_type=content_type)

    # 5. اختيار النوع (فيديو / ملفات)
    if chapter_id:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    for c in s["chapters"]:
                        if c["id"] == chapter_id:
                            return render_template('index.html', view='select_type', chapter=c)

    # 4. اختيار الشبتر (الجديد)
    if subject_id:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        return render_template('index.html', view='chapters', subject=s)

    # 3. اختيار المادة
    if year and term:
        if year in DATA and term in DATA[year]["terms"]:
            return render_template('index.html', view='subjects', 
                                   year_name=DATA[year]["name"], 
                                   term_name=DATA[year]["terms"][term]["name"], 
                                   subjects=DATA[year]["terms"][term]["subjects"],
                                   y_id=year)

    # 2. اختيار الترم
    if year:
        if year in DATA:
            return render_template('index.html', view='terms', year_name=DATA[year]["name"], terms=DATA[year]["terms"], y_id=year)

    # 1. اختيار السنة
    return render_template('index.html', view='years', data=DATA)

if __name__ == '__main__':
    app.run()
