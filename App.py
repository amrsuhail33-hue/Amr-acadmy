from flask import Flask, render_template

app = Flask(__name__)

# قاعدة بيانات بسيطة وسهلة التعديل
DATA = {
    "1": {
        "name": "السنة الأولى",
        "subjects": [
            {
                "title": "الرياضيات الهندسية",
                "video": "https://youtube.com",
                "pdf": "https://drive.google.com"
            },
            {
                "title": "الفيزياء العامة",
                "video": "https://youtube.com",
                "pdf": "https://drive.google.com"
            }
        ]
    },
    "2": {
        "name": "السنة الثانية",
        "subjects": [
            {
                "title": "إلكترونيات 1",
                "video": "https://youtube.com",
                "pdf": "https://drive.google.com"
            },
            {
                "title": "آلات كهربائية (Machines)",
                "video": "https://youtube.com",
                "pdf": "https://drive.google.com"
            }
        ]
    }
}

@app.route('/')
def home():
    return render_template('index.html', data=DATA)

if __name__ == '__main__':
    app.run(debug=True)
