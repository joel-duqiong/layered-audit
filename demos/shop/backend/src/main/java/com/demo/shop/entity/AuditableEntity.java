package com.demo.shop.entity;

import jakarta.persistence.Column;
import jakarta.persistence.MappedSuperclass;
import jakarta.persistence.PrePersist;
import jakarta.persistence.PreUpdate;
import java.time.OffsetDateTime;

/** 中间层：加上「可审计」语义 —— 谁改的、什么时候改的。 */
@MappedSuperclass
public abstract class AuditableEntity extends BaseEntity {
    @Column(length = 64)
    protected String updatedBy;

    public String getUpdatedBy() { return updatedBy; }
    public void setUpdatedBy(String updatedBy) { this.updatedBy = updatedBy; }

    /** 约定：touch 只负责记录审计信息，不做别的。 */
    public void touch(String actor) {
        this.updatedBy = actor;
    }

    @PrePersist
    @PreUpdate
    void stamp() {
        OffsetDateTime now = OffsetDateTime.now();
        if (createdAt == null) { createdAt = now; }
        updatedAt = now;
    }
}
