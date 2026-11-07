package com.demo.shop.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.FetchType;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;

@Entity
public class ShopProduct extends AuditableEntity {
    @Column(nullable = false, length = 128)
    private String name;

    @Column(nullable = false)
    private Long priceCent;

    @Column(nullable = false)
    private Integer stock;

    @ManyToOne(fetch = FetchType.LAZY, optional = false)
    @JoinColumn(name = "category_id")
    private ShopCategory category;

    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
    public Long getPriceCent() { return priceCent; }
    public void setPriceCent(Long priceCent) { this.priceCent = priceCent; }
    public Integer getStock() { return stock; }
    public void setStock(Integer stock) { this.stock = stock; }
    public ShopCategory getCategory() { return category; }
    public void setCategory(ShopCategory category) { this.category = category; }
}
