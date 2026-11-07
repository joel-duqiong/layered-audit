package com.demo.shop.client;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 跨仓库客户端 —— 调另一个仓库（python-svc）拿汇率/风控。
 *
 * 这是「跨仓库」的第一形态：进程外、跨语言、只靠 HTTP 契约。
 */
@Component
public class FxClient {
    private final HttpClient http = HttpClient.newHttpClient();
    private final String baseUrl;

    public FxClient(@Value("${app.fx-base-url:http://127.0.0.1:8098}") String baseUrl) {
        this.baseUrl = baseUrl;
    }

    public long rate(String currency) {
        try {
            HttpRequest req = HttpRequest.newBuilder()
                    .uri(URI.create(baseUrl + "/fx?currency=" + currency))
                    .GET().build();
            HttpResponse<String> resp = http.send(req, HttpResponse.BodyHandlers.ofString());
            String body = resp.body();
            int at = body.indexOf("\"rate\":");
            if (at < 0) {
                return 1L;
            }
            String tail = body.substring(at + 7).replaceAll("[^0-9]", "");
            return tail.isEmpty() ? 1L : Long.parseLong(tail);
        } catch (Exception ex) {
            return 1L;
        }
    }
}
