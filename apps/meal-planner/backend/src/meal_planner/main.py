from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from meal_planner.routers import users, foods, meal_plans, chat, meals

app = FastAPI(title="Meal Planner API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8081", "http://localhost:8082", "http://localhost:19006", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router)
app.include_router(foods.router)
app.include_router(meal_plans.router)
app.include_router(chat.router)
app.include_router(meals.router)


@app.get("/health")
def health():
    return {"status": "ok"}
