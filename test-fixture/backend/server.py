"""Backend fixture — intentional perf anti-patterns for Stage 4f rule tests.

Each block triggers exactly one rule. Comments mark the expected rule_id.
Do NOT "fix" these — the fixture exists so backend_rules.py regression tests
stay honest. Mirrors the style of the frontend fixture under `app/`.
"""
from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio
import httpx
import requests
import stripe

app = FastAPI()


# ── PLANT: backend.pydantic_complex_model ────────────────────────────────────
class UserProfile(BaseModel):
    name: str
    avatar: Optional[str] = None
    preferences: List[str] = []
    history: List[str] = []
    metadata: Optional[dict] = None


# ── PLANT: backend.sync_route_handler — hot-path name (severity HIGH) ────────
@app.get("/users/list")
def list_users():
    return {"users": []}


# ── PLANT: backend.sync_route_handler — non-hot-path (severity MEDIUM) ───────
@app.post("/admin/cleanup")
def admin_cleanup():
    return {"ok": True}


# ── PLANT: backend.blocking_work_in_handler ──────────────────────────────────
@app.post("/notify")
async def notify(payload: dict):
    # Synchronous payment provider call inline — blocks the response.
    stripe.PaymentIntent.create(amount=payload["amount"])
    return {"queued": True}


# ── PLANT: backend.mongo_client_not_singleton ────────────────────────────────
@app.get("/cart/{user_id}")
async def get_cart(user_id: str):
    client = AsyncIOMotorClient("mongodb://localhost:27017")  # new client per request
    db = client.shop
    return await db.carts.find_one({"user_id": user_id})


# ── PLANT: backend.n_plus_one_query ──────────────────────────────────────────
async def aggregate_orders(order_ids: list[str], db):
    results = []
    for oid in order_ids:
        order = await db.orders.find_one({"_id": oid})  # query inside for-loop → N+1
        results.append(order)
    return results


# ── PLANT: backend.unbounded_query ───────────────────────────────────────────
async def list_all_products(db):
    cursor = db.products.find({"status": "active"})  # no .limit, no bounded to_list
    return await cursor.to_list(length=None)


# ── PLANT: database.missing_index ────────────────────────────────────────────
# Queries on `user_id`, `created_at`, `status` but no create_index for them.
async def lookups(db):
    a = await db.events.find({"user_id": "u1", "status": "open"}).to_list(100)
    b = await db.events.find({"created_at": "2026-06-01"}).to_list(100)
    return a, b


# ── PLANT: backend.sequential_await_chain ────────────────────────────────────
async def dashboard(db, user_id: str):
    user = await db.users.find_one({"_id": user_id})
    orders = await db.orders.find({"user_id": user_id}).to_list(20)
    notifications = await db.notifications.find({"user_id": user_id}).to_list(20)
    settings = await db.settings.find_one({"user_id": user_id})
    return {"user": user, "orders": orders, "notifications": notifications, "settings": settings}


# ── PLANT: backend.no_projection_on_query ────────────────────────────────────
async def list_emails(db):
    return await db.users.find({"active": True}).to_list(50)
