select
    customer_id,
    name as customer_name,
    email as customer_email,
    created_at
from {{ ref('raw_customers') }}
