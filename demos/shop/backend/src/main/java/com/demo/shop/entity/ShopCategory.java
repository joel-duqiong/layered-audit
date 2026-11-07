package com.demo.shop.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;

@Entity
public class ShopCategory extends AuditableEntity {
    @Column(nullable = false, length = 64)
    private String name;

    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
}
