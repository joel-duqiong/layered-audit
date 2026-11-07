package com.demo.shop.web;

import com.demo.shop.service.ReportService;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/reports")
public class ReportController {
    private final ReportService reportService;

    public ReportController(ReportService reportService) {
        this.reportService = reportService;
    }

    @GetMapping("/aggregate")
    public List<String> aggregate(@RequestParam(required = false) String category,
                                  @RequestParam(defaultValue = "false") boolean onlyInStock) {
        return reportService.aggregate(category, onlyInStock);
    }
}
