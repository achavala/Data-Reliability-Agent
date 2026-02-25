select
    o.order_id,
    o.customer_id,
    c.customer_name,
    c.customer_email,
    o.order_date,
    o.order_total,
    o.status
from {{ ref('stg_orders') }} o
left join {{ ref('stg_customers') }} c
    on o.customer_id = c.customer_id
