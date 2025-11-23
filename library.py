from flask import Flask, request, redirect, url_for, render_template_string, send_file, flash
import sqlite3
import csv
import io
import os
from werkzeug.utils import secure_filename

# Configuration (can be overridden with environment variables)
DB_FILE = os.environ.get('DATABASE_PATH', 'library.db')
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
ALLOWED_EXT = {'csv'}

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'dev-secret')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure folders exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

db_dir = os.path.dirname(DB_FILE) or '.'
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

# --- Database helper ---
class LibraryDB:
    def __init__(self, db_file=DB_FILE):
        self.db_file = db_file
        self._ensure()

    def _conn(self):
        # check_same_thread=False so the connection is usable across threads when served by gunicorn
        return sqlite3.connect(self.db_file, check_same_thread=False)

    def _ensure(self):
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    author TEXT,
                    year INTEGER,
                    isbn TEXT,
                    status TEXT DEFAULT 'available',
                    issued_to TEXT
                )
            ''')
            conn.commit()

    def _to_int(self, val):
        try:
            return int(val)
        except Exception:
            return None

    def add_book(self, title, author, year, isbn):
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute('INSERT INTO books (title, author, year, isbn) VALUES (?, ?, ?, ?)',
                        (title, author, year or None, isbn))
            conn.commit()
            return cur.lastrowid

    def update_book(self, book_id, title, author, year, isbn, status=None, issued_to=None):
        book_id = self._to_int(book_id)
        if book_id is None:
            return
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(
                'UPDATE books SET title=?, author=?, year=?, isbn=?, status=?, issued_to=? WHERE id=?',
                (title, author, year or None, isbn, status or 'available', issued_to, book_id)
            )
            conn.commit()

    def delete_book(self, book_id):
        book_id = self._to_int(book_id)
        if book_id is None:
            return
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute('DELETE FROM books WHERE id=?', (book_id,))
            conn.commit()

    def get_book(self, book_id):
        book_id = self._to_int(book_id)
        if book_id is None:
            return None
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute('SELECT id, title, author, year, isbn, status, issued_to FROM books WHERE id=?', (book_id,))
            return cur.fetchone()

    def list_all(self):
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute('SELECT id, title, author, year, isbn, status, issued_to FROM books ORDER BY id')
            return cur.fetchall()

    def search(self, title=None, author=None):
        clauses = []
        params = []
        if title:
            clauses.append('title LIKE ?')
            params.append(f'%{title}%')
        if author:
            clauses.append('author LIKE ?')
            params.append(f'%{author}%')
        q = 'SELECT id, title, author, year, isbn, status, issued_to FROM books'
        if clauses:
            q += ' WHERE ' + ' AND '.join(clauses)
        q += ' ORDER BY id'
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(q, params)
            return cur.fetchall()

    def issue_book(self, book_id, issued_to):
        book = self.get_book(book_id)
        if not book:
            raise ValueError('Book not found')
        if book[5] == 'issued':
            raise ValueError('Book already issued')
        self.update_book(book[0], book[1], book[2], book[3], book[4], status='issued', issued_to=issued_to)

    def return_book(self, book_id):
        book = self.get_book(book_id)
        if not book:
            raise ValueError('Book not found')
        if book[5] == 'available':
            raise ValueError('Book is not issued')
        self.update_book(book[0], book[1], book[2], book[3], book[4], status='available', issued_to=None)

    def import_csv_fileobj(self, fileobj):
        # fileobj is a binary stream; wrap it for text reading
        text_wrapper = io.TextIOWrapper(fileobj, encoding='utf-8')
        reader = csv.DictReader(text_wrapper)
        added = 0
        for r in reader:
            title = r.get('title','').strip()
            if not title:
                continue
            author = r.get('author','').strip()
            year_raw = r.get('year','').strip()
            try:
                year = int(year_raw) if year_raw else None
            except ValueError:
                year = None
            isbn = r.get('isbn','').strip()
            self.add_book(title, author, year, isbn)
            added += 1
        try:
            text_wrapper.detach()
        except Exception:
            pass
        return added

    def export_csv_bytes(self):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['id','title','author','year','isbn','status','issued_to'])
        for row in self.list_all():
            writer.writerow([row[0], row[1] or '', row[2] or '', row[3] or '', row[4] or '', row[5] or '', row[6] or ''])
        return io.BytesIO(output.getvalue().encode('utf-8'))

# create DB instance
db = LibraryDB()

# --- Templates (single-file) ---
BASE = '''
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Library Manager</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; }
    table { border-collapse: collapse; width: 100%; }
    th, td { padding: 8px 10px; border: 1px solid #ddd; }
    th { background: #f4f4f4; text-align:left; }
    form.inline { display:inline; }
    .actions button { margin-right:6px; }
    .flash { color: green; }
    .error { color: red; }
    .container { display:flex; gap:20px; }
    .panel { padding:12px; border:1px solid #eee; border-radius:6px; background:#fafafa; }
    input[type=text], input[type=number] { width: 100%; padding:6px; margin-bottom:8px; }
    .small { width:120px; }
  </style>
</head>
<body>
  <h1>Library Management (Web)</h1>
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}
      {% for cat, msg in messages %}
        <div class="{{'error' if cat=='error' else 'flash'}}">{{ msg }}</div>
      {% endfor %}
    {% endif %}
  {% endwith %}
  <div class="container">
    <div class="panel" style="flex:1; max-width:380px;">
      <h3>Add / Edit Book</h3>
      <form method="post" action="{{ url_for('save_book') }}">
        <input type="hidden" name="id" value="{{ book.id if book else '' }}">
        <label>Title</label>
        <input name="title" type="text" value="{{ book.title if book else '' }}" required>
        <label>Author</label>
        <input name="author" type="text" value="{{ book.author if book else '' }}">
        <label>Year</label>
        <input name="year" type="number" value="{{ book.year if book else '' }}">
        <label>ISBN</label>
        <input name="isbn" type="text" value="{{ book.isbn if book else '' }}">
        <div style="display:flex; gap:8px; margin-top:8px;">
          <button type="submit">Save</button>
          <a href="{{ url_for('index') }}"><button type="button">Clear</button></a>
        </div>
      </form>

      <hr>
      <h4>Import / Export</h4>
      <form method="post" action="{{ url_for('import_csv') }}" enctype="multipart/form-data">
        <input type="file" name="file" accept=".csv">
        <div style="margin-top:8px;"><button type="submit">Import CSV</button></div>
      </form>
      <div style="margin-top:8px;">
        <a href="{{ url_for('export_csv') }}"><button>Download CSV</button></a>
      </div>
    </div>

    <div class="panel" style="flex:3;">
      <form method="get" action="{{ url_for('index') }}" style="margin-bottom:10px; display:flex; gap:8px; align-items:center;">
        <input name="title" placeholder="Search title" value="{{ request.args.get('title','') }}">
        <input name="author" placeholder="Search author" value="{{ request.args.get('author','') }}">
        <button type="submit">Search</button>
        <a href="{{ url_for('index') }}"><button type="button">Show All</button></a>
      </form>

      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Title</th>
            <th>Author</th>
            <th>Year</th>
            <th>ISBN</th>
            <th>Status</th>
            <th>Issued To</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {% for b in books %}
          <tr>
            <td>{{ b[0] }}</td>
            <td>{{ b[1] }}</td>
            <td>{{ b[2] or '' }}</td>
            <td>{{ b[3] or '' }}</td>
            <td>{{ b[4] or '' }}</td>
            <td>{{ b[5] }}</td>
            <td>{{ b[6] or '' }}</td>
            <td class="actions">
              <form method="get" action="{{ url_for('index') }}" class="inline">
                <input type="hidden" name="edit" value="{{ b[0] }}">
                <button type="submit">Edit</button>
              </form>
              <form method="post" action="{{ url_for('delete_book', book_id=b[0]) }}" class="inline" onsubmit="return confirm('Delete?');">
                <button type="submit">Delete</button>
              </form>
              {% if b[5] == 'available' %}
                <form method="post" action="{{ url_for('issue_book', book_id=b[0]) }}" class="inline">
                  <input name="issued_to" placeholder="Name" required>
                  <button type="submit">Issue</button>
                </form>
              {% else %}
                <form method="post" action="{{ url_for('return_book', book_id=b[0]) }}" class="inline">
                  <button type="submit">Return</button>
                </form>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</body>
</html>
'''

# --- Routes ---
@app.route('/')
def index():
    title = request.args.get('title')
    author = request.args.get('author')
    edit_id = request.args.get('edit')
    book = None
    if edit_id:
        book_row = db.get_book(edit_id)
        if book_row:
            book = type('B', (), { 'id': book_row[0], 'title': book_row[1], 'author': book_row[2], 'year': book_row[3], 'isbn': book_row[4] })
    if title or author:
        books = db.search(title=title, author=author)
    else:
        books = db.list_all()
    return render_template_string(BASE, books=books, book=book)

@app.route('/save', methods=['POST'])
def save_book():
    book_id = request.form.get('id')
    title = request.form.get('title','').strip()
    author = request.form.get('author','').strip()
    year = request.form.get('year','').strip()
    isbn = request.form.get('isbn','').strip()
    if not title:
        flash('Title is required', 'error')
        return redirect(url_for('index'))
    try:
        y = int(year) if year else None
    except ValueError:
        flash('Year must be a number', 'error')
        return redirect(url_for('index'))
    if book_id:
        # ensure integer
        try:
            bid = int(book_id)
        except Exception:
            bid = None
        existing = db.get_book(bid)
        status = existing[5] if existing else 'available'
        issued_to = existing[6] if existing else None
        db.update_book(bid, title, author, y, isbn, status=status, issued_to=issued_to)
        flash('Book updated')
    else:
        db.add_book(title, author, y, isbn)
        flash('Book added')
    return redirect(url_for('index'))

@app.route('/delete/<int:book_id>', methods=['POST'])
def delete_book(book_id):
    db.delete_book(book_id)
    flash('Book deleted')
    return redirect(url_for('index'))

@app.route('/issue/<int:book_id>', methods=['POST'])
def issue_book(book_id):
    name = request.form.get('issued_to','').strip()
    if not name:
        flash('Name required to issue', 'error')
        return redirect(url_for('index'))
    try:
        db.issue_book(book_id, name)
        flash('Book issued')
    except Exception as e:
        flash(str(e), 'error')
    return redirect(url_for('index'))

@app.route('/return/<int:book_id>', methods=['POST'])
def return_book(book_id):
    try:
        db.return_book(book_id)
        flash('Book returned')
    except Exception as e:
        flash(str(e), 'error')
    return redirect(url_for('index'))

@app.route('/import', methods=['POST'])
def import_csv():
    if 'file' not in request.files:
        flash('No file uploaded', 'error')
        return redirect(url_for('index'))
    f = request.files['file']
    if f.filename == '':
        flash('No file selected', 'error')
        return redirect(url_for('index'))
    filename = secure_filename(f.filename)
    ext = filename.rsplit('.',1)[1].lower() if '.' in filename else ''
    if ext in ALLOWED_EXT:
        try:
            added = db.import_csv_fileobj(f.stream)
            flash(f'Imported {added} rows')
        except Exception as e:
            flash(str(e), 'error')
    else:
        flash('Only CSV files allowed', 'error')
    return redirect(url_for('index'))

@app.route('/export')
def export_csv():
    bio = db.export_csv_bytes()
    bio.seek(0)
    return send_file(bio, as_attachment=True, download_name='library_export.csv', mimetype='text/csv')

@app.route('/health')
def health():
    return 'OK', 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() in ('1','true','yes')
    app.run(host='0.0.0.0', port=port, debug=debug)
