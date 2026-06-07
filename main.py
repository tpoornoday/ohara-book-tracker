"""
Ohara — Book Tracker
==============================
A cozy book tracking application built with FastAPI, featuring:
- Session-based authentication with admin/invite user model
- Open Library integration for book search
- Three-state book lifecycle: Want to Read → Currently Reading → Already Read
- Keep-alive health endpoint for Render free tier
"""

import os
import sqlite3
import urllib.parse
import secrets
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field
import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import bcrypt

# ──────────────────────────────────────────────
#  App Initialization
# ──────────────────────────────────────────────

app = FastAPI(title="Ohara")

# Session middleware for cookie-based auth
# max_age = 90 days (7,776,000 seconds) — keeps user logged in for ~3 months
# The SECRET_KEY signs the session cookie so it can't be tampered with
SECRET_KEY = os.environ.get("SECRET_KEY", "ohara-dev-secret-change-in-production")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    max_age=7776000,          # 90 days in seconds
    session_cookie="ohara_session",
    same_site="lax",          # Prevents CSRF from cross-site POSTs
    https_only=False,         # Set True in production with HTTPS
)

# Determine paths relative to this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "books.db")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Mount static files
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Configure Jinja2 templates
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Database Connection configuration
DATABASE_URL = os.environ.get("DATABASE_URL")


# ──────────────────────────────────────────────
#  Database Layer
# ──────────────────────────────────────────────

def get_db_connection():
    """
    Dynamically returns a database connection.
    If DATABASE_URL is set, returns a pg8000 connection to PostgreSQL.
    Otherwise, returns an sqlite3 connection.
    """
    if DATABASE_URL:
        db_url = DATABASE_URL
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)

        url = urllib.parse.urlparse(db_url)
        username = url.username
        password = url.password
        database = url.path[1:]  # strip leading slash
        hostname = url.hostname
        port = url.port or 5432

        ssl_ctx = None
        if hostname not in ("localhost", "127.0.0.1"):
            import ssl
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        import pg8000.dbapi
        return pg8000.dbapi.connect(
            user=username,
            password=password,
            host=hostname,
            port=port,
            database=database,
            ssl_context=ssl_ctx
        )
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def run_query(query: str, params: tuple = (), fetch: bool = False, fetch_one: bool = False):
    """
    Executes a query and handles database connections dynamically.
    Translates SQLite placeholder (?) to PostgreSQL (%s) format when necessary.
    """
    is_postgres = bool(DATABASE_URL)

    if is_postgres:
        query = query.replace("?", "%s")

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)

        if fetch:
            columns = [col[0] for col in cursor.description]
            if fetch_one:
                row = cursor.fetchone()
                if row:
                    return dict(row) if not is_postgres else dict(zip(columns, row))
                return None
            else:
                rows = cursor.fetchall()
                results = []
                for row in rows:
                    results.append(dict(row) if not is_postgres else dict(zip(columns, row)))
                return results
        else:
            conn.commit()
            return cursor.rowcount
    finally:
        conn.close()


# ──────────────────────────────────────────────
#  Database Initialization & Migration
# ──────────────────────────────────────────────

def init_db():
    """
    Initializes the database — creates tables and handles migrations.
    
    Migration strategy:
    - For new installs: creates tables with the final schema
    - For existing installs: uses rename-recreate-copy pattern for SQLite
      (SQLite doesn't support ALTER TABLE for constraints/primary keys)
    """
    if DATABASE_URL:
        _init_db_postgres()
    else:
        _init_db_sqlite()


def _init_db_sqlite():
    """SQLite-specific initialization with full migration support."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Create users table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(255) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Check if books table already exists with the OLD schema
    existing = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='books'"
    ).fetchone()

    if existing:
        schema_sql = existing[0] or ""
        needs_migration = "user_id" not in schema_sql

        if needs_migration:
            print("Migrating books table to new schema (adding user_id, updating constraints)...")
            # 1. Rename old table
            cursor.execute("ALTER TABLE books RENAME TO books_old")
            # 2. Create new table with updated schema
            cursor.execute("""
                CREATE TABLE books (
                    id VARCHAR(255) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    authors TEXT,
                    cover_url TEXT,
                    status VARCHAR(50) NOT NULL DEFAULT 'to_read',
                    user_id INTEGER,
                    PRIMARY KEY (id, user_id)
                )
            """)
            # 3. Copy data (user_id will be NULL — assigned to admin during /setup)
            cursor.execute("""
                INSERT INTO books (id, title, authors, cover_url, status, user_id)
                SELECT id, title, authors, cover_url, status, NULL
                FROM books_old
            """)
            # 4. Drop old table
            cursor.execute("DROP TABLE books_old")
            print(f"Migration complete. Preserved existing books.")
    else:
        # Fresh install — create table directly
        cursor.execute("""
            CREATE TABLE books (
                id VARCHAR(255) NOT NULL,
                title VARCHAR(255) NOT NULL,
                authors TEXT,
                cover_url TEXT,
                status VARCHAR(50) NOT NULL DEFAULT 'to_read',
                user_id INTEGER,
                pages INTEGER,
                date_started TEXT,
                date_finished TEXT,
                PRIMARY KEY (id, user_id)
            )
        """)

    # Fast column additions for SQLite if they don't exist
    if existing:
        schema_sql = existing[0] or ""
        if "pages" not in schema_sql:
            print("Migrating books table to add stats columns...")
            cursor.execute("ALTER TABLE books ADD COLUMN pages INTEGER")
            cursor.execute("ALTER TABLE books ADD COLUMN date_started TEXT")
            cursor.execute("ALTER TABLE books ADD COLUMN date_finished TEXT")

    conn.commit()
    conn.close()


def _init_db_postgres():
    """PostgreSQL-specific initialization."""
    # Create users table
    run_query("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(255) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create books table
    run_query("""
        CREATE TABLE IF NOT EXISTS books (
            id VARCHAR(255) NOT NULL,
            title VARCHAR(255) NOT NULL,
            authors TEXT,
            cover_url TEXT,
            status VARCHAR(50) NOT NULL DEFAULT 'to_read',
            user_id INTEGER,
            pages INTEGER,
            date_started TEXT,
            date_finished TEXT,
            PRIMARY KEY (id, user_id)
        )
    """)

    # Migration: Add user_id if missing
    try:
        result = run_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='books' AND column_name='user_id'",
            fetch=True
        )
        if not result:
            run_query("ALTER TABLE books ADD COLUMN user_id INTEGER")
    except Exception as e:
        print(f"Migration (user_id): {e}")

    # Migration: Add stats columns
    try:
        result = run_query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='books' AND column_name='pages'",
            fetch=True
        )
        if not result:
            run_query("ALTER TABLE books ADD COLUMN pages INTEGER")
            run_query("ALTER TABLE books ADD COLUMN date_started TEXT")
            run_query("ALTER TABLE books ADD COLUMN date_finished TEXT")
    except Exception as e:
        print(f"Migration (stats columns): {e}")

    # Migration: Update status constraint to allow 'reading'
    try:
        run_query("ALTER TABLE books DROP CONSTRAINT IF EXISTS books_status_check")
        run_query(
            "ALTER TABLE books ADD CONSTRAINT books_status_check "
            "CHECK (status IN ('read', 'to_read', 'reading'))"
        )
    except Exception as e:
        print(f"Migration (status constraint): {e}")


@app.on_event("startup")
def startup_event():
    try:
        init_db()
        print("Database initialization successful.")
    except Exception as e:
        print(f"CRITICAL ERROR during database initialization: {str(e)}")


# ──────────────────────────────────────────────
#  Pydantic Schemas
# ──────────────────────────────────────────────

class BookUpsert(BaseModel):
    id: str
    title: str
    authors: List[str]
    cover_url: Optional[str] = ""
    status: Literal['read', 'to_read', 'reading']
    pages: Optional[int] = None
    date_started: Optional[str] = None
    date_finished: Optional[str] = None


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=4)


# ──────────────────────────────────────────────
#  Auth Helpers
# ──────────────────────────────────────────────

def get_current_user(request: Request) -> Optional[dict]:
    """
    Reads user info from the session cookie.
    Returns user dict if logged in, None otherwise.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = run_query(
        "SELECT id, username, is_admin FROM users WHERE id = ?",
        (user_id,), fetch=True, fetch_one=True
    )
    return user


def require_auth(request: Request) -> dict:
    """Dependency that requires authentication. Returns user or redirects."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(request: Request) -> dict:
    """Dependency that requires admin privileges."""
    user = require_auth(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ──────────────────────────────────────────────
#  Auth Routes
# ──────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Serves the login page. Redirects to home if already logged in."""
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """
    Validates credentials and creates a session.
    
    How password verification works:
    1. We fetch the stored bcrypt hash from the database
    2. bcrypt.verify() hashes the submitted password with the same salt
    3. If the hashes match → password is correct → create session
    """
    user = run_query(
        "SELECT id, username, password_hash, is_admin FROM users WHERE username = ?",
        (username,), fetch=True, fetch_one=True
    )

    if not user or not bcrypt.checkpw(password.encode('utf-8'), user["password_hash"].encode('utf-8')):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid username or password"},
            status_code=401
        )

    # Set session — this creates the signed cookie
    request.session["user_id"] = user["id"]
    request.session["username"] = user["username"]
    request.session["is_admin"] = user["is_admin"]

    return RedirectResponse(url="/", status_code=302)


@app.post("/logout")
async def logout(request: Request):
    """Clears the session cookie, logging the user out."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """
    First-time setup page — only shown when no users exist.
    The first user created becomes the admin.
    """
    existing_users = run_query("SELECT COUNT(*) as cnt FROM users", fetch=True, fetch_one=True)
    if existing_users and existing_users.get("cnt", 0) > 0:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@app.post("/setup")
async def setup_admin(request: Request, username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    """Creates the admin account during first-time setup."""
    # Check no users exist yet
    existing_users = run_query("SELECT COUNT(*) as cnt FROM users", fetch=True, fetch_one=True)
    if existing_users and existing_users.get("cnt", 0) > 0:
        return RedirectResponse(url="/login", status_code=302)

    if password != confirm_password:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Passwords do not match"},
            status_code=400
        )

    if len(username) < 3:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Username must be at least 3 characters"},
            status_code=400
        )

    if len(password) < 4:
        return templates.TemplateResponse(
            request, "setup.html",
            {"error": "Password must be at least 4 characters"},
            status_code=400
        )

    # Hash password with bcrypt and create admin user
    password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    run_query(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
        (username, password_hash, True)
    )

    # Get the newly created admin's ID
    admin = run_query(
        "SELECT id FROM users WHERE username = ?",
        (username,), fetch=True, fetch_one=True
    )

    # Migrate existing books to admin user
    if admin:
        run_query(
            "UPDATE books SET user_id = ? WHERE user_id IS NULL",
            (admin["id"],)
        )

    # Auto-login the new admin
    request.session["user_id"] = admin["id"]
    request.session["username"] = username
    request.session["is_admin"] = True

    return RedirectResponse(url="/", status_code=302)


# ──────────────────────────────────────────────
#  Admin: User Management
# ──────────────────────────────────────────────

@app.post("/api/admin/users")
async def create_user(request: Request, user_data: UserCreate):
    """Admin-only: Creates a new user account."""
    admin = require_admin(request)

    # Check if username already exists
    existing = run_query(
        "SELECT id FROM users WHERE username = ?",
        (user_data.username,), fetch=True, fetch_one=True
    )
    if existing:
        raise HTTPException(status_code=400, detail="Username already exists")

    password_hash = bcrypt.hashpw(user_data.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    run_query(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
        (user_data.username, password_hash, False)
    )

    return {"status": "success", "message": f"User '{user_data.username}' created successfully"}


@app.get("/api/admin/users")
async def list_users(request: Request):
    """Admin-only: Lists all user accounts."""
    admin = require_admin(request)
    users = run_query(
        "SELECT id, username, is_admin, created_at FROM users",
        fetch=True
    )
    return users


@app.delete("/api/admin/users/{user_id}")
async def delete_user(request: Request, user_id: int):
    """Admin-only: Deletes a user and all their books."""
    admin = require_admin(request)

    if user_id == admin["id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own admin account")

    # Delete user's books first, then the user
    run_query("DELETE FROM books WHERE user_id = ?", (user_id,))
    rowcount = run_query("DELETE FROM users WHERE id = ?", (user_id,))

    if rowcount == 0:
        raise HTTPException(status_code=404, detail="User not found")

    return {"status": "success", "message": "User and their books deleted"}


# ──────────────────────────────────────────────
#  Health / Keep-Alive Endpoint
# ──────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """
    Lightweight health check endpoint.
    Called by the browser at random intervals (8-13 min)
    to prevent Render free tier from spinning down.
    """
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


# ──────────────────────────────────────────────
#  Book Search (Open Library Only)
# ──────────────────────────────────────────────

@app.get("/api/search")
async def search_books(request: Request, q: str = Query(..., min_length=1)):
    """
    Searches books via the Open Library Search API.
    
    Open Library is free, open-source, and maintained by the Internet Archive.
    No API key required. We add a User-Agent header to get 3x rate limits
    (3 req/sec instead of 1 req/sec).
    """
    # Require authentication
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    open_library_url = "https://openlibrary.org/search.json"

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                open_library_url,
                params={
                    "q": q,
                    "limit": 10,
                    "fields": "key,title,author_name,cover_i,first_publish_year,publisher,subject,number_of_pages_median,ratings_average,isbn"
                },
                headers={
                    "User-Agent": "OharaBookTracker/1.0 (personal project)"
                },
                timeout=8.0
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for doc in data.get("docs", []):
                book_key = doc.get("key", "")
                book_id = book_key.split("/")[-1] if "/" in book_key else book_key or f"ol-{doc.get('cover_i', secrets.token_hex(4))}"

                title = doc.get("title", "Unknown Title")
                authors = doc.get("author_name", [])

                # Construct cover image URL from Open Library cover ID
                cover_i = doc.get("cover_i")
                cover_url = f"https://covers.openlibrary.org/b/id/{cover_i}-M.jpg" if cover_i else ""

                # Additional metadata
                first_publish_year = doc.get("first_publish_year")
                publishers = doc.get("publisher", [])
                subjects = doc.get("subject", [])[:5]  # Limit to 5 subjects
                pages = doc.get("number_of_pages_median")
                rating = doc.get("ratings_average")
                isbn_list = doc.get("isbn", [])

                results.append({
                    "id": book_id,
                    "title": title,
                    "authors": authors,
                    "cover_url": cover_url,
                    "first_publish_year": first_publish_year,
                    "publishers": publishers[:3] if publishers else [],
                    "subjects": subjects,
                    "pages": pages,
                    "rating": round(rating, 1) if rating else None,
                    "isbn": isbn_list[0] if isbn_list else None,
                })
            return results

        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Open Library search failed: {str(e)}"
            )


# ──────────────────────────────────────────────
#  Book CRUD Endpoints
# ──────────────────────────────────────────────

@app.get("/api/books")
def get_books(request: Request):
    """Fetches all saved books for the current logged-in user."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        rows = run_query(
            "SELECT id, title, authors, cover_url, status, pages, date_started, date_finished FROM books WHERE user_id = ?",
            (user["id"],), fetch=True
        )
        books = []
        for row in rows:
            authors_list = [a.strip() for a in row["authors"].split(",")] if row["authors"] else []
            books.append({
                "id": row["id"],
                "title": row["title"],
                "authors": authors_list,
                "cover_url": row["cover_url"],
                "status": row["status"],
                "pages": row["pages"],
                "date_started": row["date_started"],
                "date_finished": row["date_finished"]
            })
        return books
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database retrieval failed: {str(e)}")


@app.post("/api/books")
def upsert_book(request: Request, book: BookUpsert):
    """
    Inserts a book or updates its status if the ID already exists.
    Books are scoped to the current user.
    """
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        authors_str = ", ".join(book.authors)
        user_id = user["id"]

        # Check if the book already exists for this user
        existing = run_query(
            "SELECT status FROM books WHERE id = ? AND user_id = ?",
            (book.id, user_id), fetch=True, fetch_one=True
        )

        if existing:
            run_query(
                "UPDATE books SET title = ?, authors = ?, cover_url = ?, status = ?, pages = ?, date_started = ?, date_finished = ? WHERE id = ? AND user_id = ?",
                (book.title, authors_str, book.cover_url, book.status, book.pages, book.date_started, book.date_finished, book.id, user_id)
            )
            message = "Book updated successfully"
        else:
            run_query(
                "INSERT INTO books (id, title, authors, cover_url, status, user_id, pages, date_started, date_finished) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (book.id, book.title, authors_str, book.cover_url, book.status, user_id, book.pages, book.date_started, book.date_finished)
            )
            message = "Book added successfully"

        return {"status": "success", "message": message, "book_id": book.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database upsert failed: {str(e)}")


@app.delete("/api/books/{id}")
def delete_book(request: Request, id: str):
    """Deletes a book from the current user's collection."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        rowcount = run_query(
            "DELETE FROM books WHERE id = ? AND user_id = ?",
            (id, user["id"])
        )

        if rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Book with ID {id} not found")
        return {"status": "success", "message": "Book deleted successfully"}
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database deletion failed: {str(e)}")


# ──────────────────────────────────────────────
#  HTML Frontend Routes
# ──────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    """
    Serves the main application page.
    Redirects to /setup if no users exist, or /login if not authenticated.
    """
    # Check if any users exist — if not, redirect to first-time setup
    user_count = run_query("SELECT COUNT(*) as cnt FROM users", fetch=True, fetch_one=True)
    if not user_count or user_count.get("cnt", 0) == 0:
        return RedirectResponse(url="/setup", status_code=302)

    # Check if user is logged in
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(request, "index.html", {
        "user": user
    })
