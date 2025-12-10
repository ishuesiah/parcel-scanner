# Code Patterns Reference

Detailed before/after examples for pragmatic refactoring. Load this file when reviewing or refactoring code that exhibits anti-patterns.

## Train Wreck Refactoring

### Before: Deep coupling through chained access
```python
def process_order(customer_id, discount_code):
    # Bad: Knows too much about internal structure
    customer = db.customers.get(customer_id)
    order = customer.orders.current()
    items = order.line_items.all()
    subtotal = order.pricing.calculator.compute_subtotal(items)
    discount = promotions.engine.discounts.lookup(discount_code)
    final = order.pricing.calculator.apply_discount(subtotal, discount)
    order.totals.final_amount = final
    order.status.state_machine.transition_to("confirmed")
    return order.serialize.to_json()
```

### After: Each object handles its own responsibilities
```python
def process_order(customer_id, discount_code):
    # Good: Tell objects what to do, don't reach into their internals
    customer = Customer.find(customer_id)
    order = customer.current_order()
    order.apply_discount(discount_code)
    order.confirm()
    return order.to_json()
```

**Key insight:** The Order class now handles pricing, discounts, and status internally. Callers don't need to know about pricing calculators or state machines.

---

## Broken Window Examples

### Magic Numbers
```python
# Bad: What do these numbers mean?
if user.age >= 18 and order.total >= 50 and len(items) <= 10:
    apply_discount(0.15)

# Good: Named constants explain intent
MINIMUM_AGE_FOR_DISCOUNT = 18
MINIMUM_ORDER_VALUE = 50
MAXIMUM_ITEMS_FOR_EXPRESS = 10
EXPRESS_DISCOUNT_RATE = 0.15

if (user.age >= MINIMUM_AGE_FOR_DISCOUNT and 
    order.total >= MINIMUM_ORDER_VALUE and 
    len(items) <= MAXIMUM_ITEMS_FOR_EXPRESS):
    apply_discount(EXPRESS_DISCOUNT_RATE)
```

### Copy-Paste Code
```python
# Bad: Same validation logic repeated
def create_customer(data):
    if not data.get('email') or '@' not in data['email']:
        raise ValueError("Invalid email")
    if not data.get('name') or len(data['name']) < 2:
        raise ValueError("Invalid name")
    # ... create customer

def update_customer(customer_id, data):
    if not data.get('email') or '@' not in data['email']:
        raise ValueError("Invalid email")
    if not data.get('name') or len(data['name']) < 2:
        raise ValueError("Invalid name")
    # ... update customer

# Good: Extract shared validation
def validate_customer_data(data):
    """Validate customer data fields. Raises ValueError if invalid."""
    if not data.get('email') or '@' not in data['email']:
        raise ValueError("Invalid email")
    if not data.get('name') or len(data['name']) < 2:
        raise ValueError("Invalid name")

def create_customer(data):
    validate_customer_data(data)
    # ... create customer

def update_customer(customer_id, data):
    validate_customer_data(data)
    # ... update customer
```

### TODO Comments Without Tracking
```python
# Bad: TODOs that will never get done
def calculate_shipping(order):
    # TODO: Add international shipping support
    # TODO: Handle oversized items
    # TODO: Add express shipping option
    return 5.99  # flat rate for now

# Good: Minimal implementation with tracked issues
def calculate_shipping(order):
    """Calculate shipping cost.
    
    Currently flat-rate domestic only.
    See issues #142 (international), #143 (oversized), #144 (express)
    """
    return Decimal("5.99")
```

---

## Decoupling Patterns

### Hard-coded Dependencies
```python
# Bad: Tightly coupled to specific implementations
class OrderProcessor:
    def __init__(self):
        self.db = PostgresDatabase()
        self.emailer = SendGridClient()
        self.payment = StripeGateway()
    
    def process(self, order):
        self.db.save(order)
        self.payment.charge(order.total)
        self.emailer.send_confirmation(order)

# Good: Dependencies injected, easy to test and swap
class OrderProcessor:
    def __init__(self, db, payment_gateway, notification_service):
        self.db = db
        self.payment = payment_gateway
        self.notifier = notification_service
    
    def process(self, order):
        self.db.save(order)
        self.payment.charge(order.total)
        self.notifier.send_confirmation(order)

# Usage - production
processor = OrderProcessor(
    db=PostgresDatabase(config.db_url),
    payment_gateway=StripeGateway(config.stripe_key),
    notification_service=SendGridClient(config.sendgrid_key)
)

# Usage - testing
processor = OrderProcessor(
    db=InMemoryDatabase(),
    payment_gateway=MockPaymentGateway(),
    notification_service=MockNotifier()
)
```

### Feature Flags for Easier Change
```python
# Pattern: Isolate new behavior behind flags for safe rollout
class ShippingCalculator:
    def __init__(self, feature_flags):
        self.flags = feature_flags
    
    def calculate(self, order):
        if self.flags.is_enabled("new_shipping_algorithm"):
            return self._calculate_v2(order)
        return self._calculate_v1(order)
    
    def _calculate_v1(self, order):
        # Original implementation
        ...
    
    def _calculate_v2(self, order):
        # New implementation - can be tested in production
        # with limited rollout before full switch
        ...
```

---

## Deliberate Programming Checks

### Understanding Your Code
```python
# Before committing, can you answer these?

# 1. WHY does this regex work?
pattern = r'^(?=.*[A-Z])(?=.*[a-z])(?=.*\d).{8,}$'
# Answer: Requires uppercase, lowercase, digit, 8+ chars via lookaheads

# 2. WHAT assumptions does this rely on?
def get_user_timezone(request):
    return request.headers.get('X-Timezone', 'UTC')
# Assumes: Frontend always sends header, UTC is safe default,
# header value is valid timezone string

# 3. WHAT happens with unexpected input?
def calculate_average(numbers):
    return sum(numbers) / len(numbers)
# Problem: Crashes on empty list! 

# Fixed:
def calculate_average(numbers):
    if not numbers:
        return 0  # or raise ValueError("Cannot average empty list")
    return sum(numbers) / len(numbers)
```

---

## Refactoring Workflow

### Safe Refactoring Steps

```python
# Step 1: Identify the code smell
# This function does too many things
def process_user_registration(data):
    # Validate
    if not data['email']: raise ValueError()
    if not data['password']: raise ValueError()
    if len(data['password']) < 8: raise ValueError()
    
    # Hash password
    salt = generate_salt()
    hashed = hash_password(data['password'], salt)
    
    # Create user
    user = User(email=data['email'], password_hash=hashed, salt=salt)
    db.save(user)
    
    # Send email
    template = load_template('welcome.html')
    body = template.render(name=data['name'])
    send_email(data['email'], 'Welcome!', body)
    
    return user

# Step 2: Write tests for current behavior FIRST
def test_registration_validates_email():
    with pytest.raises(ValueError):
        process_user_registration({'email': '', 'password': 'valid123'})

def test_registration_creates_user():
    result = process_user_registration({
        'email': 'test@example.com', 
        'password': 'valid123',
        'name': 'Test'
    })
    assert result.email == 'test@example.com'

# Step 3: Extract one piece at a time, running tests after each
def validate_registration_data(data):
    if not data.get('email'): 
        raise ValueError("Email required")
    if not data.get('password'): 
        raise ValueError("Password required")
    if len(data['password']) < 8: 
        raise ValueError("Password must be 8+ characters")

def create_user_record(email, password):
    salt = generate_salt()
    hashed = hash_password(password, salt)
    return User(email=email, password_hash=hashed, salt=salt)

def send_welcome_email(email, name):
    template = load_template('welcome.html')
    body = template.render(name=name)
    send_email(email, 'Welcome!', body)

# Step 4: Compose the clean version
def process_user_registration(data):
    validate_registration_data(data)
    user = create_user_record(data['email'], data['password'])
    db.save(user)
    send_welcome_email(data['email'], data['name'])
    return user
```

---

## Error Handling Philosophy

### Dead Programs Tell Tales
```python
# Bad: Silently swallowing errors
def fetch_user_data(user_id):
    try:
        response = api.get(f'/users/{user_id}')
        return response.json()
    except:
        return {}  # Silent failure - caller has no idea something went wrong

# Good: Fail fast with useful information
def fetch_user_data(user_id):
    try:
        response = api.get(f'/users/{user_id}')
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as e:
        logger.error(f"Failed to fetch user {user_id}: {e}")
        raise UserDataError(f"Could not retrieve user {user_id}") from e
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON for user {user_id}: {e}")
        raise UserDataError(f"Invalid response for user {user_id}") from e
```

### Appropriate Error Granularity
```python
# Define errors at the right level of abstraction
class OrderError(Exception):
    """Base class for order-related errors."""
    pass

class InsufficientInventoryError(OrderError):
    """Raised when items are out of stock."""
    def __init__(self, item_id, requested, available):
        self.item_id = item_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"Item {item_id}: requested {requested}, only {available} available"
        )

class PaymentDeclinedError(OrderError):
    """Raised when payment cannot be processed."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"Payment declined: {reason}")

# Callers can catch at appropriate level
try:
    process_order(order)
except InsufficientInventoryError as e:
    notify_user_backorder(e.item_id)
except PaymentDeclinedError as e:
    prompt_alternative_payment(e.reason)
except OrderError as e:
    # Catch-all for order issues
    log_and_notify_support(e)
```
