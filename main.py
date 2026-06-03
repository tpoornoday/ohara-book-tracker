import os
import sqlite3
import urllib.parse
from typing import List, Literal, Optional
from pydantic import BaseModel
import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# Initialize FastAPI App
app = FastAPI(title="Ohara")

# Determine paths relative to this file
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "books.db")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

# Configure Jinja2 templates
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Database Connection configuration
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db_connection():
    """
    Dynamically returns a database connection.
    If DATABASE_URL is set, returns a pg8000 connection to PostgreSQL.
    Otherwise, returns an sqlite3 connection.
    """
    if DATABASE_URL:
        # Standardize URL prefix
        db_url = DATABASE_URL
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
            
        url = urllib.parse.urlparse(db_url)
        username = url.username
        password = url.password
        database = url.path[1:] # strip leading slash
        hostname = url.hostname
        port = url.port or 5432
        
        # Configure SSL context for cloud databases (e.g., Supabase, Neon)
        # Enable SSL by default for remote connections
        ssl_ctx = None
        if hostname not in ("localhost", "127.0.0.1"):
            import ssl
            ssl_ctx = ssl.create_default_context()
            
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
    Returns list/dict of rows for SELECTs, or row count for modifying queries.
    """
    is_postgres = bool(DATABASE_URL)
    
    # Translate parameter placeholders if using PostgreSQL (pg8000 uses %s)
    if is_postgres:
        query = query.replace("?", "%s")
        
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(query, params)
        
        if fetch:
            # Map column names for a unified dictionary interface
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

# Database setup on startup
def init_db():
    """Initializes the database and creates the books table if it doesn't exist."""
    # This DDL is fully ANSI SQL compatible, working in both SQLite and PostgreSQL
    query = """
        CREATE TABLE IF NOT EXISTS books (
            id VARCHAR(255) PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            authors TEXT,
            cover_url TEXT,
            status VARCHAR(50) NOT NULL CHECK(status IN ('read', 'to_read'))
        )
    """
    run_query(query)

@app.on_event("startup")
def startup_event():
    init_db()

# Pydantic Schemas for validation
class BookUpsert(BaseModel):
    id: str
    title: str
    authors: List[str]
    cover_url: Optional[str] = ""
    status: Literal['read', 'to_read']

# API Endpoint 1: GET /api/search?q=...
@app.get("/api/search")
async def search_books(q: str = Query(..., min_length=1)):
    """
    Proxies to the public Google Books API and parses the response to return
    clean book information. Rewrites HTTP thumbnail URLs to HTTPS.
    Handles rate-limiting or network issues gracefully.
    """
    google_books_url = "https://www.googleapis.com/books/v1/volumes"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(google_books_url, params={"q": q, "maxResults": 8}, timeout=6.0)
            if response.status_code == 429:
                # Handle rate limiting gracefully
                raise HTTPException(
                    status_code=429, 
                    detail="Google Books API is currently rate-limiting search requests. Please try again later or add custom book IDs."
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise HTTPException(status_code=e.response.status_code, detail=f"Google Books API error: {e.response.text}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Unexpected error calling search API: {str(e)}")

        data = response.json()
        results = []
        for item in data.get("items", []):
            volume_info = item.get("volumeInfo", {})
            book_id = item.get("id")
            title = volume_info.get("title", "Unknown Title")
            authors = volume_info.get("authors", [])
            
            # Extract thumbnail and guarantee HTTPS to prevent mixed content issues
            image_links = volume_info.get("imageLinks", {})
            cover_url = image_links.get("thumbnail") or image_links.get("smallThumbnail") or ""
            if cover_url and cover_url.startswith("http://"):
                cover_url = cover_url.replace("http://", "https://", 1)
                
            results.append({
                "id": book_id,
                "title": title,
                "authors": authors,
                "cover_url": cover_url
            })
            
        return results

# API Endpoint 2: GET /api/books
@app.get("/api/books")
def get_books():
    """Fetches all saved books from the SQLite or PostgreSQL database."""
    try:
        rows = run_query("SELECT id, title, authors, cover_url, status FROM books", fetch=True)
        books = []
        for row in rows:
            # Reconstruct list of authors from comma-separated string
            authors_list = [a.strip() for a in row["authors"].split(",")] if row["authors"] else []
            books.append({
                "id": row["id"],
                "title": row["title"],
                "authors": authors_list,
                "cover_url": row["cover_url"],
                "status": row["status"]
            })
        return books
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database retrieval failed: {str(e)}")

# API Endpoint 3: POST /api/books
@app.post("/api/books")
def upsert_book(book: BookUpsert):
    """
    Inserts a book or updates its status if the ID already exists in the database.
    Works natively on both SQLite and PostgreSQL.
    """
    try:
        # Serialize authors list to a comma-separated string
        authors_str = ", ".join(book.authors)
        
        # Check if the book already exists
        existing = run_query("SELECT status FROM books WHERE id = ?", (book.id,), fetch=True, fetch_one=True)
        
        if existing:
            # Update all fields if book exists to support full editing
            run_query(
                "UPDATE books SET title = ?, authors = ?, cover_url = ?, status = ? WHERE id = ?",
                (book.title, authors_str, book.cover_url, book.status, book.id)
            )
            message = "Book updated successfully"
        else:
            # Insert new book record
            run_query(
                "INSERT INTO books (id, title, authors, cover_url, status) VALUES (?, ?, ?, ?, ?)",
                (book.id, book.title, authors_str, book.cover_url, book.status)
            )
            message = "Book added successfully"
            
        return {"status": "success", "message": message, "book_id": book.id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database upsert failed: {str(e)}")

# API Endpoint 4: DELETE /api/books/{id}
@app.delete("/api/books/{id}")
def delete_book(id: str):
    """Deletes a book from the database by ID."""
    try:
        rowcount = run_query("DELETE FROM books WHERE id = ?", (id,))
        
        if rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Book with ID {id} not found")
        return {"status": "success", "message": "Book deleted successfully"}
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database deletion failed: {str(e)}")

# HTML Frontend Serving Route
@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request):
    """Serves the single-page HTML frontend."""
    return templates.TemplateResponse(request, "index.html")
