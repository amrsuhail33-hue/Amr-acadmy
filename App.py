import csv
import io
import requests
from flask import Flask, render_template, request

app = Flask(__name__)

# تم وضع رابط الشيت الخاص بك وتحويله لصيغة القراءة المباشرة
CSV_URL = "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv"

def get_data_from_sheet():
    data = {}
    if not CSV_URL.startswith("http"):
        return data

    try:
        response = requests.get(CSV_URL)
        response.encoding = 'utf-8'
        csv_reader = csv.reader(io.StringIO(response.text))
        
        # تخطي الصف الأول (عناوين الأعمدة)
        next(csv_reader, None)

        for row in csv_reader:
            # حماية من الأخطاء في حال كان الصف غير مكتمل
            year = row[0].strip() if len(row) > 0 else ""
            term = row[1].strip() if len(row) > 1 else ""
            subject = row[2].strip() if len(row) > 2 else ""
            title = row[3].strip() if len(row) > 3 else ""
            video_url = row[4].strip() if len(row) > 4 else ""
            download_url = row[5].strip() if len(row) > 5 else ""
            content_type = row[6].strip() if len(row) > 6 else ""
            file_url = row[7].strip() if len(row) > 7 else ""

            # تجاهل الصفوف الفارغة
            if not year or not term or not subject or not title:
                continue

            # بناء الهيكلة تلقائياً
            if year not in data:
                data[year] = {"name": year, "terms": {}}
            
            if term not in data[year]["terms"]:
                data[year]["terms"][term] = {"name": term, "subjects": {}}
            
            if subject not in data[year]["terms"][term]["subjects"]:
                subj_id = f"{year}_{term}_{subject}".replace(" ", "_")
                data[year]["terms"][term]["subjects"][subject] = {
                    "id": subj_id,
                    "name": subject,
                    "videos": [],
                    "explanations": []
                }

            # فرز المحتوى (فيديو أو ملف)
            if content_type == "فيديو":
                # استخدم رابط اليوتيوب أولاً، وإن كان فارغاً استخدم رابط التيليجرام
                final_url = video_url if video_url else download_url
                if final_url:
                    data[year]["terms"][term]["subjects"][subject]["videos"].append({"title": title, "url": final_url})
            
            elif content_type == "ملف":
                if file_url:
                    data[year]["terms"][term]["subjects"][subject]["explanations"].append({"title": title, "url": file_url})

        # تحويل القاموس إلى قائمة للواجهة
        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())

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

    if subject_id and content_type:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        return render_template('index.html', view='content', subject=s, content_type=content_type)

    if subject_id:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        return render_template('index.html', view='select_type', subject=s)

    if year and term:
        if year in DATA and term in DATA[year]["terms"]:
            return render_template('index.html', view='subjects', 
                                   year_name=DATA[year]["name"], 
                                   term_name=DATA[year]["terms"][term]["name"], 
                                   subjects=DATA[year]["terms"][term]["subjects"],
                                   y_id=year)

    if year:
        if year in DATA:
            return render_template('index.html', view='terms', year_name=DATA[year]["name"], terms=DATA[year]["terms"], y_id=year)

    return render_template('index.html', view='years', data=DATA)

if __name__ == '__main__':
    app.run()
