from flask import Flask, request, render_template_string
import google.generativeai as genai
import PIL.Image
import os

app = Flask(__name__)

# إعداد مفتاح الـ API
genai.configure(api_key="AQ.Ab8RN6InBf7Ye3SqIseKfXrD1Ceu7XqCI_nbeEDTlhj29oo3Gg")
model = genai.GenerativeModel('gemini-1.5-flash')

# صفحة تجريبية بسيطة
HTML_PAGE = """
<div dir="rtl" style="font-family: Arial; padding: 20px;">
    <h1>اختبار ذكاء الـ AI</h1>
    <form action="/analyze" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept="image/*">
        <button type="submit">حلل الصورة</button>
    </form>
</div>
"""

@app.route('/')
def index():
    return HTML_PAGE

@app.route('/analyze', methods=['POST'])
def analyze():
    file = request.files['file']
    temp_path = "test_img.jpg"
    file.save(temp_path)
    
    img = PIL.Image.open(temp_path)
    response = model.generate_content(["اشرح لي ما تراه في هذه الصورة بوضوح", img])
    
    return f"<h3>النتيجة:</h3><p>{response.text}</p>"

if __name__ == '__main__':
    app.run(debug=True, port=5001) # استخدمنا بورت 5001 لكي لا يتعارض مع موقعك الأساسي
