<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Amr Suhail Academy</title>
    <link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;900&display=swap" rel="stylesheet">
    <style>
        * { -webkit-tap-highlight-color: transparent; box-sizing: border-box; font-family: 'Tajawal', sans-serif; }
        body { 
            background-color: #f8fafc; 
            margin: 0; padding: 0; color: #0f172a; 
            display: flex; flex-direction: column; min-height: 100vh;
        }
        
        .navbar {
            position: sticky; top: 0; background: rgba(255, 255, 255, 0.9);
            backdrop-filter: blur(12px); border-bottom: 1px solid #e2e8f0;
            display: flex; justify-content: space-between; align-items: center;
            padding: 15px 5%; z-index: 100; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);
        }
        .navbar-brand { font-size: 1.4rem; font-weight: 900; color: #0f172a; text-decoration: none; letter-spacing: 0.5px; }
        .navbar-contact {
            background-color: #0f172a; color: #ffffff; padding: 8px 18px;
            border-radius: 50px; text-decoration: none; font-size: 0.95rem; font-weight: 700;
            transition: background-color 0.3s;
        }
        .navbar-contact:hover { background-color: #334155; }

        .main-content { flex: 1; width: 100%; max-width: 1000px; margin: 0 auto; padding: 40px 20px; }
        
        .hero-section { text-align: center; margin-bottom: 40px; }
        .hero-title { font-size: 2.2rem; font-weight: 900; color: #0f172a; margin: 0 0 10px 0; }
        .hero-subtitle { font-size: 1.1rem; color: #64748b; margin: 0; font-weight: 500; }

        .breadcrumb { 
            display: flex; justify-content: center; gap: 8px; margin-bottom: 40px; 
            font-size: 0.95rem; font-weight: 700; color: #64748b;
        }
        .breadcrumb a { color: #2563eb; text-decoration: none; padding: 4px 12px; background: #eff6ff; border-radius: 20px; transition: 0.2s; }
        .breadcrumb a:hover { background: #dbeafe; color: #1d4ed8; }
        .breadcrumb span { margin-top: 4px; }
        
        .grid-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 20px; }
        .card { 
            background: #ffffff; padding: 30px 20px; border-radius: 16px; text-align: center; 
            text-decoration: none; font-size: 1.25rem; font-weight: 800; color: #0f172a;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -1px rgba(0,0,0,0.03);
            border: 1px solid #f1f5f9; transition: all 0.3s ease;
        }
        .card:hover { transform: translateY(-5px); box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1), 0 10px 10px -5px rgba(0,0,0,0.04); border-color: #e2e8f0; }
        
        .card-primary { background-color: #2563eb; color: #ffffff; border: none; }
        .card-primary:hover { box-shadow: 0 20px 25px -5px rgba(37,99,235,0.2); }
        .card-secondary { background-color: #f8fafc; color: #0f172a; border: 1px solid #cbd5e1; }
        
        .section-header { text-align: center; font-size: 1.8rem; color: #0f172a; margin: 0 0 30px 0; font-weight: 900; }
        
        .content-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 15px; margin-bottom: 40px; }
        .item-btn { 
            display: flex; align-items: center; justify-content: center; padding: 14px 16px; 
            border-radius: 10px; text-decoration: none; font-size: 1rem; font-weight: 700; 
            text-align: center; transition: all 0.2s ease; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .item-btn:hover { transform: translateY(-2px); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        
        .btn-video { background-color: #fee2e2; color: #991b1b; border: 1px solid #fecaca; }
        .btn-video:hover { background-color: #fecaca; }
        .btn-pdf { background-color: #d1fae5; color: #065f46; border: 1px solid #a7f3d0; }
        .btn-pdf:hover { background-color: #a7f3d0; }

        .footer { background-color: #0f172a; color: #94a3b8; text-align: center; padding: 40px 20px; margin-top: auto; }
        .footer-brand { font-size: 1.4rem; font-weight: 900; color: #ffffff; margin-bottom: 10px; }
        .footer-contact { margin-top: 15px; font-size: 0.95rem; }
        .footer-contact a { color: #38bdf8; text-decoration: none; font-weight: 700; }
    </style>
</head>
<body>

    <nav class="navbar">
        <a href="/" class="navbar-brand">Amr Suhail Academy</a>
        <a href="https://wa.me/967774485611" target="_blank" class="navbar-contact">تواصل معنا</a>
    </nav>

    <div class="main-content">
        
        {% if view == 'years' %}
        <div class="hero-section">
            <h1 class="hero-title">بوابتك للتفوق الهندسي</h1>
            <p class="hero-subtitle">اختر السنة الدراسية للبدء</p>
        </div>
        {% endif %}

        {% if view != 'years' %}
        <div class="breadcrumb">
            <a href="/">الرئيسية</a>
            {% if view in ['subjects', 'chapters', 'select_type', 'content'] %}
                <span>/</span> <a href="javascript:history.back()">رجوع للخلف</a>
            {% endif %}
        </div>
        {% endif %}

        <!-- 1. السنة -->
        {% if view == 'years' %}
        <div class="grid-container">
            {% for id, year in data.items() %}
                <a href="/?year={{ id }}" class="card card-primary">{{ year.name }}</a>
            {% endfor %}
        </div>
        {% endif %}

        <!-- 2. الترم -->
        {% if view == 'terms' %}
        <div class="grid-container">
            {% for t_id, term_data in terms.items() %}
            <a href="/?year={{ y_id }}&term={{ t_id }}" class="card">{{ term_data.name }}</a>
            {% endfor %}
        </div>
        {% endif %}

        <!-- 3. المادة -->
        {% if view == 'subjects' %}
        <div class="grid-container">
            {% for subject in subjects %}
                <a href="/?subject={{ subject.id }}" class="card">{{ subject.name }}</a>
            {% endfor %}
        </div>
        {% endif %}

        <!-- 4. اختيار النوع بعد المادة مباشرة -->
        {% if view == 'select_type' %}
        <h2 class="section-header">{{ subject.name }}</h2>
        <div class="grid-container">
            <a href="/?subject={{ subject.id }}&type=videos" class="card card-secondary">مقاطع الفيديو والشروحات</a>
            <a href="/?subject={{ subject.id }}&type=files" class="card card-secondary">الملفات والمستندات</a>
        </div>
        {% endif %}

        <!-- 5. عرض الشباتر بناء على النوع المختار -->
        {% if view == 'chapters' %}
        <h2 class="section-header">{{ subject.name }}</h2>
        <div class="grid-container">
            {% for chapter in chapters %}
                <a href="/?subject={{ subject.id }}&type={{ content_type }}&chapter={{ chapter.name }}" class="card">{{ chapter.name }}</a>
            {% endfor %}
        </div>
        {% endif %}

        <!-- 6. عرض الروابط داخل الشبتر -->
        {% if view == 'content' %}
        <h2 class="section-header">{{ subject.name }} - {{ chapter.name }}</h2>
        <div class="content-grid">
            {% for item in chapter.items %}
                <a href="{{ item.url }}" target="_blank" class="item-btn {% if content_type == 'videos' %}btn-video{% else %}btn-pdf{% endif %}">{{ item.title }}</a>
            {% endfor %}
        </div>
        {% endif %}

    </div>

    <footer class="footer">
        <div class="footer-brand">Amr Suhail Academy</div>
        <div>جميع الحقوق محفوظة &copy; 2026</div>
        <div class="footer-contact">
            للدعم والاستفسار: <a href="https://wa.me/967774485611" target="_blank" dir="ltr">+967 774 485 611</a>
        </div>
    </footer>

</body>
</html>
