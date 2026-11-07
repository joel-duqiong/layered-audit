package com.demo.shop.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;

@Entity
public class ShopCustomer extends AuditableEntity {
    @Column(nullable = false, length = 64)
    private String name;

    /** 会员等级：VIP / NORMAL。 */
    @Column(nullable = false, length = 16)
    private String level;

    /** 授信额度（分）。 */
    @Column(nullable = false)
    private Long creditLimitCent;

    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
    public String getLevel() { return level; }
    public void setLevel(String level) { this.level = level; }
    public Long getCreditLimitCent() { return creditLimitCent; }
    public void setCreditLimitCent(Long v) { this.creditLimitCent = v; }
}
