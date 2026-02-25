select
    order_id,
    customer_id,
    order_date,
    amount as order_total,
    status
from {{ ref('raw_orders') }}
