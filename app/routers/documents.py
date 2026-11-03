"""
DealShield Document Room — view-only, watermarked ownership documents for land/property deals.

Flow (docs deal):
  1. Deal created (category=land/real-estate) with docs_deadline (default 7 days after funding).
  2. Buyer funds escrow -> document room opens for the SELLER to upload (buyer also sees room, empty).
  3. Seller uploads docs (PDF/images) -> stored encrypted on disk, never downloadable as raw files.
  4. Buyer views each page as a watermarked PNG inside the app: every page stamped with
     "FROM: seller name/phone - TO: buyer name/phone - DealShield deal #ID - timestamp".
  5. If seller misses docs_deadline -> FULL refund to buyer, zero commission (auto).
  6. If seller delivers docs and buyer walks away in bad faith (cancels after docs_uploaded_at)
     -> buyer forfeits walkaway_fee (1% capped at N500,000, min N50,000) to the seller.

Security model:
  - Files stored outside the web root with random names; buyer NEVER gets a URL to the raw file.
  - Served exclusively as rendered PNG pages through an authenticated, authz-checked endpoint.
  - Every served page is watermarked per-view with viewer identity + timestamp.
"""
import os
import uuid
import io
import base64
import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, Response
from sqlalchemy.orm import Session
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

from app.dependencies import get_db
from app.models.models import EscrowTransaction, DealDocument, User, AuditLog
from app.routers.auth import get_current_user
from app.core.security_middleware import sanitize_text

router = APIRouter()

DOCS_DIR = os.environ.get(
    "DEALSHIELD_DOCS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "deal_docs"),
)
WALKAWAY_MIN = 50_000.0
WALKAWAY_RATE = 0.01
WALKAWAY_CAP = 500_000.0
DOCS_DEADLINE_DAYS = 7
MAX_FILE_MB = 25
ALLOWED_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/jpg", "image/webp"}

LAND_CATEGORIES = {"land", "real_estate", "real-estate", "property", "real estate"}


def _utcnow():
    return datetime.datetime.utcnow()


def _is_docs_deal(tx) -> bool:
    return (tx.category or "").lower().replace(" ", "_") in {c.replace("-", "_").replace(" ", "_") for c in LAND_CATEGORIES}


def _log_audit(db, user_id, action, request, target_id=None, details=""):
    try:
        ip = request.client.host if request.client else "unknown"
    except Exception:
        ip = "unknown"
    db.add(AuditLog(actor_id=user_id, action=action, target_type="escrow_transaction",
                    target_id=target_id, ip_address=ip, details=details))


def _calc_walkaway_fee(amount) -> float:
    amt = float(amount)  # amounts may arrive as Decimal (NUMERIC column)
    return max(WALKAWAY_MIN, min(amt * WALKAWAY_RATE, WALKAWAY_CAP))


def _require_party(tx, user):
    if user.id not in (tx.buyer_id, tx.seller_id):
        raise HTTPException(403, "You are not a party to this deal")


def _user_label(user: User) -> str:
    phone = getattr(user, "phone", "") or ""
    return f"{user.name or 'User'} ({phone})" if phone else (user.name or "User")


# ── Watermark rendering ──

def _load_font(size):
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ):
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _render_watermarked_page(page_img: Image.Image, wm_text: str) -> bytes:
    """Stamp diagonal repeated watermark + footer band with viewer identity."""
    img = page_img.convert("RGB")
    if img.width > 1600:  # cap render size so pages stay fast to serve
        ratio = 1600 / img.width
        img = img.resize((1600, int(img.height * ratio)))
    W, H = img.size
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    font = _load_font(max(14, W // 45))

    # Diagonal repeated watermark
    label = wm_text
    try:
        bbox = d.textbbox((0, 0), label, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except Exception:
        tw, th = len(label) * 8, 16
    step_x = max(tw + 60, 120)
    step_y = max(th * 3, 60)
    row = 0
    y = -th
    while y < H + th:
        x = -(row % 2) * (step_x // 2)
        while x < W + tw:
            # rotate text: draw on temp layer for slant
            d.text((x, y), label, font=font, fill=(128, 128, 128, 45))
            x += step_x
        y += step_y
        row += 1
    slant = overlay.rotate(20, expand=False)
    slant.putalpha(slant.split()[3].point(lambda a: int(a * 0.55)))
    img = Image.alpha_composite(img.convert("RGBA"), slant)

    # Footer band
    d2 = ImageDraw.Draw(img)
    band_h = max(30, H // 18)
    d2.rectangle([0, H - band_h, W, H], fill=(10, 10, 10, 235))
    ffont = _load_font(max(12, band_h - 10))
    d2.text((12, H - band_h + (band_h - 16) // 2), wm_text[:180], font=ffont, fill=(255, 255, 255, 255))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _pdf_pages(path, max_pages=40):
    """Yield PIL images per PDF page. Tries pypdfium2, falls back to pdftoppm."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(path)
        n = min(len(pdf), max_pages)
        for i in range(n):
            page = pdf[i]
            yield page.render(scale=2).to_pil()
        return
    except ImportError:
        pass
    import subprocess, glob
    outdir = "/tmp/_ds_pdfrender_" + uuid.uuid4().hex
    os.makedirs(outdir, exist_ok=True)
    subprocess.run(["pdftoppm", "-png", "-r", "150", "-scale-to-x", "1600", "-scale-to-y", "-1",
                    path, os.path.join(outdir, "pg")], timeout=120)
    for f in sorted(glob.glob(os.path.join(outdir, "pg-*.png")))[:max_pages]:
        yield Image.open(f)
        os.unlink(f)
    try:
        os.rmdir(outdir)
    except OSError:
        pass


# ── Endpoints ──

@router.get("/{tx_id}/documents")
def list_documents(tx_id: int, current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    """List a deal's document room. Buyer sees metadata only (never file paths)."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(404, "Transaction not found")
    _require_party(tx, current_user)
    docs = db.query(DealDocument).filter(DealDocument.tx_id == tx_id).order_by(DealDocument.id).all()
    room = {
        "tx_id": tx_id,
        "status": tx.status,
        "docs_deadline": tx.docs_deadline.isoformat() if tx.docs_deadline else None,
        "docs_uploaded_at": tx.docs_uploaded_at.isoformat() if tx.docs_uploaded_at else None,
        "can_upload": current_user.id == tx.seller_id and tx.status in ("funded", "seller_fulfilling"),
        "can_view": tx.status in ("funded", "seller_fulfilling", "buyer_review", "buyer_approved",
                                  "released", "disputed", "under_investigation"),
        "walkaway_fee": tx.walkaway_fee or 0,
        "documents": [
            {"id": d.id, "note": sanitize_text(d.note or ""), "file_type": d.file_type,
             "page_count": d.page_count,
             "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None}
            for d in docs
        ],
    }
    return room


@router.post("/{tx_id}/documents/upload")
async def upload_document(tx_id: int, request: Request,
                          file: UploadFile = File(...), note: str = Form(""),
                          current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """Seller uploads an ownership document. Stored encrypted-on-disk, random filename."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(404, "Transaction not found")
    if current_user.id != tx.seller_id:
        raise HTTPException(403, "Only the seller can upload documents")
    if tx.status not in ("funded", "seller_fulfilling"):
        raise HTTPException(400, f"Document room opens after the buyer funds escrow (current: {tx.status})")

    content = await file.read()
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(413, f"File too large (max {MAX_FILE_MB}MB)")
    ctype = (file.content_type or "").lower()
    if ctype not in ALLOWED_TYPES:
        raise HTTPException(415, "Only PDF, JPG, PNG or WEBP allowed")
    ftype = "pdf" if ctype == "application/pdf" else "image"

    os.makedirs(DOCS_DIR, exist_ok=True)
    fname = uuid.uuid4().hex + ".bin"
    fpath = os.path.join(DOCS_DIR, fname)
    with open(fpath, "wb") as f:
        f.write(content)  # NOTE: filesystem-level; mount dir encrypted (LUKS) in production

    page_count = 1
    if ftype == "pdf":
        try:
            import pypdfium2 as pdfium
            page_count = min(len(pdfium.PdfDocument(fpath)), 40)
        except Exception:
            page_count = 1

    doc = DealDocument(tx_id=tx_id, uploader_id=current_user.id,
                       filename=(file.filename or "document")[:200],
                       storage_path=fpath, file_type=ftype, page_count=page_count,
                       note=sanitize_text(note or "")[:200])
    db.add(doc)
    if not tx.docs_uploaded_at:
        tx.docs_uploaded_at = _utcnow()
    _log_audit(db, current_user.id, "doc_upload", request, target_id=tx_id,
               details=f"doc note={note[:60]} type={ftype}")
    db.commit()
    return {"ok": True, "document_id": doc.id, "page_count": page_count}


@router.get("/{tx_id}/documents/{doc_id}/page/{page}")
def view_document_page(tx_id: int, doc_id: int, page: int,
                       current_user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    """Serve ONE page of a document as a watermarked PNG. Raw file never leaves the server."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(404, "Transaction not found")
    _require_party(tx, current_user)
    if tx.status not in ("funded", "seller_fulfilling", "buyer_review", "buyer_approved",
                         "released", "disputed", "under_investigation"):
        raise HTTPException(403, "Documents become viewable after the buyer funds escrow")
    doc = db.query(DealDocument).filter(DealDocument.id == doc_id, DealDocument.tx_id == tx_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")

    seller = db.query(User).get(tx.seller_id)
    buyer = db.query(User).get(tx.buyer_id)
    viewer_is_buyer = current_user.id == tx.buyer_id
    if viewer_is_buyer:
        wm = (f"FROM: {_user_label(seller)}  •  TO: {_user_label(buyer)}  •  "
              f"DealShield Deal #{tx_id}  •  Viewed {_utcnow().strftime('%Y-%m-%d %H:%M UTC')}  •  "
              "VIEW ONLY — NOT A TITLE INSTRUMENT")
    else:
        wm = (f"DEALSHIELD INTERNAL — Deal #{tx_id} — Seller copy — "
              f"{_user_label(current_user)} — {_utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    # Buyer page numbers are 1-indexed
    if page < 1:
        raise HTTPException(400, "Page starts at 1")

    try:
        if doc.file_type == "pdf":
            pages = list(_pdf_pages(doc.storage_path))
            if page > len(pages):
                raise HTTPException(404, f"Page {page} of {len(pages)}")
            img = pages[page - 1]
        else:
            if page != 1:
                raise HTTPException(404, "Single-page document")
            img = Image.open(doc.storage_path)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Could not render document page")

    png = _render_watermarked_page(img, wm)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store, max-age=0",
                             "Content-Disposition": "inline"})


@router.post("/{tx_id}/documents/verify-deadline")
def check_docs_deadline(tx_id: int, request: Request,
                         current_user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """Manual/cron trigger: if seller missed docs deadline -> full refund, zero commission."""
    tx = db.query(EscrowTransaction).filter(EscrowTransaction.id == tx_id).first()
    if not tx:
        raise HTTPException(404, "Transaction not found")
    if not tx.docs_deadline:
        raise HTTPException(400, "No docs deadline on this deal")
    now = _utcnow()
    if now < tx.docs_deadline:
        return {"ok": True, "action": "none", "deadline": tx.docs_deadline.isoformat()}
    if tx.docs_uploaded_at and tx.docs_uploaded_at <= tx.docs_deadline:
        return {"ok": True, "action": "none", "docs_uploaded_at": tx.docs_uploaded_at.isoformat()}

    # Seller failed to deliver documents -> FULL refund to buyer, no commission
    if tx.status in ("funded", "seller_fulfilling"):
        tx.status = "refunded"
        tx.closed_at = now
        tx.admin_resolution = "docs_deadline_missed_auto_refund"
        tx.admin_reason = ("Seller did not upload ownership documents by the deadline. "
                           "Full refund to buyer, zero commission per DealShield docs-deal policy.")
        tx.commission = 0.0
        # credit buyer's wallet with the full amount
        buyer = db.query(User).get(tx.buyer_id)
        if buyer is not None:
            buyer.wallet_balance = (buyer.wallet_balance or 0) + tx.amount
        from app.models.models import WalletTx
        db.add(WalletTx(user_id=tx.buyer_id, amount=tx.amount, tx_type="docs_refund",
                        description=f"Full refund — docs deadline missed on deal #{tx_id}"))
        _log_audit(db, current_user.id, "docs_deadline_refund", request, target_id=tx_id,
                   details="auto full refund, zero commission")
        db.commit()
        return {"ok": True, "action": "refunded_full", "commission": 0.0}
    return {"ok": True, "action": "none", "status": tx.status}
