CREATE TABLE shop_category (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR(64)
);

CREATE TABLE shop_customer (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    level VARCHAR(16) NOT NULL,
    credit_limit_cent BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR(64),
    CONSTRAINT chk_shop_customer_credit CHECK (credit_limit_cent >= 0)
);

CREATE TABLE shop_product (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    price_cent BIGINT NOT NULL,
    stock INTEGER NOT NULL,
    category_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR(64),
    CONSTRAINT fk_shop_product_category FOREIGN KEY (category_id) REFERENCES shop_category(id),
    CONSTRAINT chk_shop_product_price CHECK (price_cent >= 0)
);

CREATE TABLE shop_order (
    id BIGSERIAL PRIMARY KEY,
    customer_name VARCHAR(64) NOT NULL,
    quoted_amount_cent BIGINT NOT NULL,
    settled_amount_cent BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by VARCHAR(64),
    CONSTRAINT chk_shop_order_quoted CHECK (quoted_amount_cent >= 0),
    CONSTRAINT chk_shop_order_settled CHECK (settled_amount_cent >= 0)
);

INSERT INTO shop_category (name) VALUES ('粮油'), ('生鲜');
INSERT INTO shop_customer (name, level, credit_limit_cent) VALUES ('demo-vip', 'VIP', 1000000), ('demo-normal', 'NORMAL', 100000);
INSERT INTO shop_product (name, price_cent, stock, category_id)
    SELECT '商品-' || g, 1000 * g, 10 * g, 1 FROM generate_series(1, 8) g;
