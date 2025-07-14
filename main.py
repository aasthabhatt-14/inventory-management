from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import mysql.connector
from datetime import date, timedelta
import pickle
import pandas as pd


# Initialize FastAPI application
app = FastAPI()

# Allow frontend connection
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Use specific domain in production
    allow_methods=["*"],
    allow_headers=["*"]
)

# DB connection config
db_config = {
    "host": "mysql.railway.internal",
    "user": "root",
    "password": "ZNlsztTLSymVendThmgZISuWaijEnyIA",
    "database": "railway",
    "port": 3306
}

# Pydantic model for request validation
class StockEntry(BaseModel):
    product_id: int
    quantity: float
    production_date: date
    expiry_date: date

# Pydantic model for product input
class ProductRequest(BaseModel):
    product_id: int

# pydantic model for prooduct update
class ProductUpdate(BaseModel):
    batch_ids: list[int]
    delivered_on: date
    quantity_removed: float

class ExpiryAlert(BaseModel):
    product_name: str
    batch_id: str
    expiry_date: str
    days_remaining: int
    severity: str

@app.post("/add-stock")
def add_stock(data: StockEntry):
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()

        query = """
            INSERT INTO inventory (product_id, production_date, expiry_date, quantity)
            VALUES (%s, %s, %s, %s)
        """

        values = (data.product_id, data.production_date, data.expiry_date, data.quantity)

        cursor.execute(query, values)
        conn.commit()
        cursor.close()
        conn.close()
        return {"message": "Stock added successfully!"}
    except Exception as e:
        return {"message": f"Error: {e}"}


@app.post("/get-batches")
def get_batches(request: ProductRequest):
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT batch_id, product_id, quantity, production_date, expiry_date
            FROM inventory
            WHERE product_id = %s
            ORDER BY expiry_date ASC
        """, (request.product_id,))

        results = cursor.fetchall()
        cursor.close()
        conn.close()

        return results

    except Exception as e:
        print("Error:", e)

@app.post("/update-stock")
def update_stock(request: ProductUpdate):
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        # Step 1: Fetch product_id from first batch (assuming all batches are same product)
        cursor.execute("SELECT product_id FROM inventory WHERE batch_id = %s", (request.batch_ids[0],))
        result = cursor.fetchone()
        if not result:
            return {"message": "Invalid batch ID"}
        product_id = result["product_id"]

        # Step 2: Delete selected batches from inventory
        format_strings = ','.join(['%s'] * len(request.batch_ids))
        cursor.execute(
            f"DELETE FROM inventory WHERE batch_id IN ({format_strings})",
            tuple(request.batch_ids)
        )

        # Step 3: Insert into sales table
        cursor.execute(
            "INSERT INTO sales (product_id, quantity, sales_date) VALUES (%s, %s, %s)",
            (product_id, request.quantity_removed, request.delivered_on)
        )

        conn.commit()
        cursor.close()
        conn.close()

        return {"message": "Stock updated and sale recorded successfully!"}

    except Exception as e:
        return {"message": f"Error: {e}"}
    
@app.get("/product-summary")
def get_product_summary():
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT p.product_id, p.product_names, p.unit, 
                   IFNULL(SUM(i.quantity), 0) AS total_quantity
            FROM products p
            LEFT JOIN inventory i ON p.product_id = i.product_id
            GROUP BY p.product_id, p.product_names, p.unit
        """)
        summary = cursor.fetchall()
        cursor.close()
        conn.close()
        return summary

    except Exception as e:
        return {"message": f"Error: {e}"}
    

# Load your XGBoost model once
with open("xgb_demand_model.pkl", "rb") as f:
    model = pickle.load(f)


@app.get("/forecast-summary")
def forecast_summary():
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        today = date.today()
        current_week = today.isocalendar()[1]

        results = []

        for product_id in range(1, 11):
            # Get product info and current stock
            cursor.execute("""
                SELECT p.product_names, p.unit, IFNULL(SUM(i.quantity), 0) as stock
                FROM products p
                LEFT JOIN inventory i ON p.product_id = i.product_id
                WHERE p.product_id = %s
                GROUP BY p.product_id
            """, (product_id,))
            prod = cursor.fetchone()
            if not prod:
                continue

            # Fetch all past sales for the product
            cursor.execute("""
                SELECT sales_date, quantity FROM sales
                WHERE product_id = %s AND sales_date <= %s
            """, (product_id, today))
            sales_rows = cursor.fetchall()


            past_sales = [0, 0, 0, 0, 0]
            past_dates = ["N/A", "N/A", "N/A", "N/A", "N/A"]

            if not sales_rows:
                lag = 0
                rolling_avg = 0
            else:
                df = pd.DataFrame(sales_rows)
                df['sales_date'] = pd.to_datetime(df['sales_date'])
                df['quantity'] = pd.to_numeric(df['quantity'], errors='coerce')
                df['year'] = df['sales_date'].dt.isocalendar().year
                df['week'] = df['sales_date'].dt.isocalendar().week
                df['sales_date'] = pd.to_datetime(df['sales_date'])
                df = df.sort_values('sales_date')


                df['year'] = df['sales_date'].dt.isocalendar().year
                df['week'] = df['sales_date'].dt.isocalendar().week
                df['week_start'] = df['sales_date'] - pd.to_timedelta(df['sales_date'].dt.weekday, unit='d')
                weekly_sales = df.groupby('week_start')['quantity'].sum().sort_index()


                # Past 5 sales entries (date + quantity)
                recent_entries = weekly_sales.tail(5)
                past_sales = recent_entries.tolist()
                past_dates = recent_entries.index.strftime('%Y-%m-%d').tolist()


                # Pad with 0s if fewer than 5
                while len(past_sales) < 5:
                    past_sales.insert(0, 0)
                    past_dates.insert(0, "N/A")


               # Prepare current and previous year-week
                prev_date = today - timedelta(weeks=1)
                if prev_date in weekly_sales.index:
                    lag = int(weekly_sales.loc[prev_date])
                else:
                    lag = int(weekly_sales.iloc[-1]) if not weekly_sales.empty else 0


                # rolling avg of up to last 4 full weeks
                rolling_weeks = weekly_sales.tail(4)
                rolling_avg = float(rolling_weeks.mean()) if not rolling_weeks.empty else float(lag)

            # Prepare input for next 5 weeks
            input_data = []
            for i in range(5):
                input_data.append({
                    "product_id": product_id,
                    "week": (current_week + i - 1) % 52 + 1,
                    "lag": lag,
                    "rolling_avg": rolling_avg
                })

            X_pred = pd.DataFrame(input_data)
            X_pred.rename(columns={
                "product_id": "Product ID",
                "week": "week",
                "lag": "Lag_1",
                "rolling_avg": "Rolling_Avg_4"
            }, inplace=True)

            forecast = model.predict(X_pred).round().astype(int).tolist()
            forecast_dates = [(today + timedelta(weeks=i)).strftime('%Y-%m-%d') for i in range(5)]
            total_forecasted = sum(forecast)

            # Determine reorder week
            cumulative_demand = 0
            reorder_week_index = None

            for i, demand in enumerate(forecast):
                cumulative_demand += demand
                if cumulative_demand >= prod["stock"]:
                    reorder_week_index = max(i - 1, 0)  # reorder one week before stock finishes
                    break

            if reorder_week_index is not None:
                reorder_date = (today + timedelta(weeks=reorder_week_index)).strftime('%Y-%m-%d')
                reorder_needed = True
            else:
                reorder_date = "N/A"
                reorder_needed = False

            # Reorder quantity
            reorder_quantity = max(total_forecasted - prod["stock"], 0)

            # Stock level
            coverage = (prod["stock"] / total_forecasted) * 100 if total_forecasted > 0 else 100
            if coverage >= 100:
                stock_level = "Sufficient"
            elif coverage >= 70:
                stock_level = "Moderate"
            else:
                stock_level = "Critical"


            results.append({
                "product_id": product_id,
                "product_name": prod["product_names"],
                "unit": prod["unit"],
                "current_stock": prod["stock"],
                "forecast": forecast,
                "forecast_dates": forecast_dates,
                "total_forecasted": total_forecasted,
                "past_sales": past_sales,
                "past_dates": past_dates,
                "week": current_week,
                "reorder_needed": reorder_needed,
                "reorder_date": reorder_date,
                "reorder_quantity": reorder_quantity,
                "stock_level": stock_level
            })



        cursor.close()
        conn.close()
        return results

    except Exception as e:
        return {"error": str(e)}
    
@app.get("/expiring-alerts")
def get_expiring_alerts():
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT i.batch_id, i.expiry_date, p.product_names AS product_name
            FROM inventory i
            JOIN products p ON i.product_id = p.product_id
            WHERE DATEDIFF(i.expiry_date, CURDATE()) <= 15
            ORDER BY i.expiry_date ASC
        """)
        
        items = cursor.fetchall()
        result = []

        for row in items:
            days_remaining = (row["expiry_date"] - date.today()).days
            severity = (
                "Critical" if days_remaining <= 3 else
                "Warning" if days_remaining <= 7 else
                "Safe"
            )

            result.append({
                "product_name": row["product_name"],
                "batch_id": row["batch_id"],
                "expiry_date": row["expiry_date"].strftime("%Y-%m-%d"),
                "days_remaining": days_remaining,
                "severity": severity
            })

        cursor.close()
        conn.close()
        return result

    except Exception as e:
        return {"error": str(e)}


@app.get("/batch-inventory")
def get_batch_inventory():
    try:
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT i.batch_id, i.quantity, i.production_date, i.expiry_date,
                   p.product_names AS product_name
            FROM inventory i
            JOIN products p ON i.product_id = p.product_id
            ORDER BY i.expiry_date ASC
        """)
        
        rows = cursor.fetchall()
        result = []
        today = date.today()

        for row in rows:
            days_to_expire = (row["expiry_date"] - today).days
            result.append({
                "product_name": row["product_name"],
                "batch_id": row["batch_id"],
                "quantity": row["quantity"],
                "production_date": row["production_date"].strftime("%Y-%m-%d"),
                "expiration_date": row["expiry_date"].strftime("%Y-%m-%d"),
                "days_to_expire": days_to_expire
            })

        cursor.close()
        conn.close()
        return result

    except Exception as e:
        return {"error": str(e)}


from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/")
def serve_home():
    return FileResponse("frontend/index.html")
