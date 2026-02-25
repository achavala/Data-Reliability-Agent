select
    c.customer_id,
    c.customer_name,
    c.customer_email,
    count(o.order_id) as total_orders,
    coalesce(sum(o.order_total), 0) as total_spent,
    min(o.order_date) as first_order_date,
    max(o.order_date) as last_order_date
from {{ ref('stg_customers') }} c
left join {{ ref('stg_orders') }} o
    on c.customer_id = o.customer_id
group by c.customer_id, c.customer_name, c.customer_email
