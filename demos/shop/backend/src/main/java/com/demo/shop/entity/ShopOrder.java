package com.demo.shop.entity;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;

@Entity
public class ShopOrder extends AuditableEntity {
    @Column(nullable = false, length = 64)
    private String customerName;

    @Column(nullable = false)
    private Long quotedAmountCent;

    @Column(nullable = false)
    private Long settledAmountCent;

    @Column(nullable = false, length = 16)
    private String status;

    /** 报价与结算额所属的币种（结算额已按该币种的汇率折算过 —— 汇率来自另一个仓库）。 */
    @Column(nullable = false, length = 8)
    private String currency;

    public String getCustomerName() { return customerName; }
    public void setCustomerName(String v) { this.customerName = v; }
    public Long getQuotedAmountCent() { return quotedAmountCent; }
    public void setQuotedAmountCent(Long v) { this.quotedAmountCent = v; }
    public Long getSettledAmountCent() { return settledAmountCent; }
    public void setSettledAmountCent(Long v) { this.settledAmountCent = v; }
    public String getStatus() { return status; }
    public void setStatus(String v) { this.status = v; }
    public String getCurrency() { return currency; }
    public void setCurrency(String v) { this.currency = v; }

    /**
     * ⚠️ 故意埋的缺陷：覆写父类 touch() 却做了不兼容的事 —— 把审计人清空。
     *
     * 父类约定 touch 只负责记录审计信息（谁改的），子类却抹掉了这个信息：
     * 任何以为 touch 之后 updatedBy 可信的代码都会读到 null。
     * 这违反里氏替换（子类覆写不能收紧/改变父类承诺的后置条件）。
     */
    @Override
    public void touch(String actor) {
        super.touch(actor);
        this.updatedBy = null;
    }
}
