# הוראות הפעלה — משרות בישראל

## מה תצטרכי לעשות (פעם אחת)

---

## שלב 1 — יצירת מסד נתונים ב-Supabase (10 דקות)

1. כנסי לאתר: https://supabase.com/dashboard
2. לחצי **Sign Up** → **Continue with Google** (כניסה עם גוגל)
3. לחצי **New project**:
   - **Name**: `job-tracker`
   - **Database Password**: תמציאי סיסמה ושמרי אותה
   - **Region**: בחרי `West EU (Frankfurt)` — הכי קרוב לישראל
   - לחצי **Create new project** (לוקח ~2 דקות)

4. כשהפרויקט מוכן — לכי לתפריט שמאל → **SQL Editor**
5. העתיקי את תוכן הקובץ `supabase_setup.sql` והדביקי בתיבה
6. לחצי **Run** ← ✅ המסד נוצר

7. עכשיו לחצי על **Settings** (גלגל שיניים בתפריט) → **API**
8. שמרי שני דברים:
   - **Project URL** — נראה כך: `https://xxxxx.supabase.co`
   - **anon public key** — מחרוזת ארוכה

---

## שלב 2 — העלאת הקוד ל-GitHub (5 דקות)

1. כנסי ל-GitHub.com → לחצי **+** למעלה → **New repository**
2. **Repository name**: `job-tracker`
3. **Public** (חשוב! כדי שהאתר יעבוד)
4. לחצי **Create repository**

5. פתחי Terminal ב-Mac (חפשי Terminal בחיפוש)
6. הדביקי פקודה אחת בכל פעם:

```bash
cd ~/גודה/job-tracker
git init
git add .
git commit -m "Initial setup"
git remote add origin https://github.com/YOUR_USERNAME/job-tracker.git
git push -u origin main
```
   (החליפי `YOUR_USERNAME` בשם המשתמש שלך ב-GitHub)

---

## שלב 3 — חיבור הסודות (Secrets) ל-GitHub (3 דקות)

1. ב-GitHub, כנסי לריפוזיטורי שלך → **Settings** → **Secrets and variables** → **Actions**
2. לחצי **New repository secret** — הוסיפי שני סודות:

   | Name | Value |
   |------|-------|
   | `SUPABASE_URL` | ה-Project URL מ-Supabase |
   | `SUPABASE_KEY` | ה-anon public key מ-Supabase |

---

## שלב 4 — עדכון האתר עם פרטי Supabase (2 דקות)

פתחי את הקובץ `web/index.html` בעורך טקסט (NotePad, TextEdit)
מצאי את השורות האלה ועדכני:

```javascript
const SUPABASE_URL = "YOUR_SUPABASE_URL";      // החליפי עם ה-URL מ-Supabase
const SUPABASE_ANON_KEY = "YOUR_SUPABASE_ANON_KEY";  // החליפי עם ה-anon key
```

שמרי את הקובץ ועשי `git add . && git commit -m "Add Supabase config" && git push`

---

## שלב 5 — הפעלת האתר (2 דקות)

1. ב-GitHub, כנסי לריפוזיטורי → **Settings** → **Pages**
2. תחת **Source** בחרי: **Deploy from a branch**
3. תחת **Branch** בחרי: `main` ואז `/web`
4. לחצי **Save**

אחרי ~1 דקה האתר יהיה זמין בכתובת:
`https://YOUR_USERNAME.github.io/job-tracker`

---

## שלב 6 — הרצה ראשונה של ה-Scraper

1. ב-GitHub, לכי ל-**Actions** (בתפריט העליון)
2. תראי את ה-workflow "Daily Job Scraper"
3. לחצי **Run workflow** → **Run workflow** (כפתור ירוק)
4. ממתינים ~10-15 דקות
5. כנסי לאתר — המשרות יופיעו! 🎉

מכאן כל יום בשעה 6 בבוקר הסקריפט ירוץ לבד.

---

## שאלות נפוצות

**למה חלק מהחברות לא מופיעות?**
חלק מהאתרים דורשים JavaScript מורכב או הרשמה — אלה לא ייאספו בגרסה הנוכחית.

**איך מוסיפים חברה חדשה?**
פתחי `scraper/companies.json` והוסיפי שורה בפורמט:
```json
{"name": "שם החברה", "url": "https://careers.company.com"}
```

**האתר לא טוען?**
בדקי שהוספת את ה-SUPABASE_URL וה-SUPABASE_ANON_KEY נכון ב-`index.html`.
