import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.jiuyan.banquet.config.ApiResponseAdvice;
import com.jiuyan.banquet.config.TraceContext;
import com.jiuyan.banquet.config.TraceIdFilter;
import com.jiuyan.banquet.config.WebConfig;
import com.jiuyan.banquet.dto.ApiResponse;
import com.jiuyan.banquet.dto.CreatePackageRequest;
import com.jiuyan.banquet.dto.PackageItemRequest;
import com.jiuyan.banquet.dto.UpdatePackageRequest;
import com.jiuyan.banquet.entity.BanquetPackage;
import com.jiuyan.banquet.entity.Category;
import com.jiuyan.banquet.entity.PackageItem;
import com.jiuyan.banquet.entity.PackageType;
import com.jiuyan.banquet.entity.Product;
import com.jiuyan.banquet.exception.BusinessException;
import com.jiuyan.banquet.exception.GlobalExceptionHandler;
import com.jiuyan.banquet.repository.BanquetPackageRepository;
import com.jiuyan.banquet.repository.PackageTypeRepository;
import com.jiuyan.banquet.repository.ProductRepository;
import com.jiuyan.banquet.service.PackageService;
import com.jiuyan.banquet.service.UploadService;
import org.slf4j.LoggerFactory;
import org.springframework.core.MethodParameter;
import org.springframework.core.io.Resource;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.converter.json.MappingJackson2HttpMessageConverter;
import org.springframework.mock.web.MockFilterChain;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.util.ResourceUtils;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.core.io.UrlResource;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.ResourceHandlerRegistry;
import org.springframework.http.server.ServletServerHttpRequest;
import org.springframework.http.server.ServletServerHttpResponse;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.converter.HttpMessageNotReadableException;

import java.io.IOException;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * 文件层执行载体：整类实例化 + 可编排的失败注入桩 + 中间态快照 + 日志捕获。
 *
 * 与函数层的区别：函数层看「一个方法算得对不对」，文件层看「一个文件里的方法编排
 * 顺不对、失败之后实体被改成什么样、资源边界是不是同一个」。
 */
public class FileHarness {

    static final ObjectMapper M = new ObjectMapper()
        .registerModule(new com.fasterxml.jackson.datatype.jsr310.JavaTimeModule());
    static final Map<Long, Product> PRODUCTS = new LinkedHashMap<>();
    static final List<String> CALL_LOG = new ArrayList<>();
    static ListAppender<ILoggingEvent> LOG_APPENDER;

    public static void main(String[] args) throws Exception {
        buildFixtures();
        attachLogCapture();
        JsonNode cases = M.readTree(Paths.get(args[0]).toFile());
        ArrayNode out = M.createArrayNode();
        for (JsonNode c : cases) {
            out.add(runScenario(c));
        }
        Files.writeString(Paths.get(args[1]), out.toString());
    }

    // ------------------------------------------------------------------ fixtures

    static void buildFixtures() {
        Category cat = new Category();
        cat.setId(1L);
        cat.setName("汤品");

        product(11L, "花胶鸡汤", 8800L, cat);
        product(13L, "红枣莲子羹", 4800L, cat);
        product(29L, "套餐外商品", 100L, cat);
    }

    static void product(long id, String name, long price, Category cat) {
        Product p = new Product();
        p.setId(id);
        p.setName(name);
        p.setPriceCent(price);
        p.setUnit("份");
        p.setCategory(cat);
        p.setImageUrl("/uploads/seed/products/product-" + id + ".jpg");
        PRODUCTS.put(id, p);
    }

    static BanquetPackage packageWithItems(long id, String name, long price, long... productIds) {
        BanquetPackage pkg = new BanquetPackage();
        pkg.setId(id);
        pkg.setName(name);
        pkg.setBanquetType("婚宴");
        pkg.setPriceCent(price);
        pkg.setIsActive(true);
        pkg.setIsDeleted(false);
        for (long pid : productIds) {
            PackageItem item = new PackageItem();
            item.setId(pid * 100);
            item.setBanquetPackage(pkg);
            item.setProduct(PRODUCTS.get(pid));
            item.setQuantity(2);
            pkg.getItems().add(item);
        }
        return pkg;
    }

    // ------------------------------------------------------------ repository stubs

    static Object defaultValue(Class<?> type) {
        if (type == Optional.class) return Optional.empty();
        if (type == boolean.class || type == Boolean.class) return Boolean.FALSE;
        if (type == int.class || type == Integer.class) return 0;
        if (type == long.class || type == Long.class) return 0L;
        if (List.class.isAssignableFrom(type)) return List.of();
        return null;
    }

    static BanquetPackageRepository packageRepo(BanquetPackage target) {
        InvocationHandler h = (proxy, method, args) -> {
            CALL_LOG.add("pkgRepo." + method.getName());
            switch (method.getName()) {
                case "findByIdAndIsDeletedFalse":
                    return Optional.ofNullable(target);
                case "save":
                    return args[0];
                default:
                    return defaultValue(method.getReturnType());
            }
        };
        return (BanquetPackageRepository) Proxy.newProxyInstance(
            FileHarness.class.getClassLoader(), new Class<?>[]{BanquetPackageRepository.class}, h);
    }

    static ProductRepository productRepo(Set<Long> missing) {
        InvocationHandler h = (proxy, method, args) -> {
            CALL_LOG.add("productRepo." + method.getName());
            if ("findByIdAndIsDeletedFalse".equals(method.getName())) {
                Long id = (Long) args[0];
                if (missing.contains(id)) return Optional.empty();
                return Optional.ofNullable(PRODUCTS.get(id));
            }
            return defaultValue(method.getReturnType());
        };
        return (ProductRepository) Proxy.newProxyInstance(
            FileHarness.class.getClassLoader(), new Class<?>[]{ProductRepository.class}, h);
    }

    static PackageTypeRepository typeRepo(Set<String> known) {
        InvocationHandler h = (proxy, method, args) -> {
            CALL_LOG.add("typeRepo." + method.getName());
            if ("findByNameAndIsDeletedFalse".equals(method.getName())) {
                String name = (String) args[0];
                if (name == null || !known.contains(name)) return Optional.empty();
                PackageType t = new PackageType();
                t.setId(1L);
                t.setName(name);
                return Optional.of(t);
            }
            return defaultValue(method.getReturnType());
        };
        return (PackageTypeRepository) Proxy.newProxyInstance(
            FileHarness.class.getClassLoader(), new Class<?>[]{PackageTypeRepository.class}, h);
    }

    // ----------------------------------------------------------------- log capture

    static void attachLogCapture() {
        LoggerContext ctx = (LoggerContext) LoggerFactory.getILoggerFactory();
        LOG_APPENDER = new ListAppender<>();
        LOG_APPENDER.setContext(ctx);
        LOG_APPENDER.start();
        ctx.getLogger("com.jiuyan.banquet").addAppender(LOG_APPENDER);
    }

    static List<String> capturedLogs() {
        List<String> out = new ArrayList<>();
        for (ILoggingEvent e : LOG_APPENDER.list) {
            out.add(e.getFormattedMessage());
        }
        return out;
    }

    static void clearLogs() {
        LOG_APPENDER.list.clear();
    }

    // ------------------------------------------------------------------ dispatcher

    static ObjectNode runScenario(JsonNode spec) {
        ObjectNode res = M.createObjectNode();
        String id = spec.path("id").asText();
        res.put("id", id);
        CALL_LOG.clear();
        clearLogs();
        try {
            switch (id) {
                case "PKG-UPDATE-SUBSET" -> pkgUpdateSubset(res, spec);
                case "PKG-UPDATE-MISSING-PRODUCT" -> pkgUpdateMissingProduct(res, spec);
                case "PKG-CREATE-MISSING-PRODUCT" -> pkgCreateMissingProduct(res, spec);
                case "PKG-NULL-BANQUET-TYPE" -> pkgNullBanquetType(res, spec);
                case "PKG-EMPTY-ITEMS" -> pkgEmptyItems(res, spec);
                case "PKG-DUPLICATE-ITEMS" -> pkgDuplicateItems(res, spec);
                case "PKG-QUANTITY-ZERO" -> pkgQuantityZero(res, spec);
                case "PKG-DUP-DB-ITEMS" -> pkgDupDbItems(res, spec);
                case "PKG-UPDATE-MISSING-PACKAGE" -> pkgUpdateMissingPackage(res, spec);
                case "CORS-ORIGIN-MATRIX" -> corsOriginMatrix(res, spec);
                case "CORS-CREDENTIALS" -> corsCredentials(res, spec);
                case "UPLOADS-RESOURCE-LOCATION" -> uploadsResourceLocation(res, spec);
                case "UPLOADS-SAME-ROOT" -> uploadsSameRoot(res, spec);
                case "TRACE-XFF-SPOOF" -> traceXffSpoof(res, spec);
                case "TRACE-ID-VERBATIM" -> traceIdVerbatim(res, spec);
                case "TRACE-MDC-CLEANUP" -> traceMdcCleanup(res, spec);
                case "ERR-DATA-INTEGRITY-LEAK" -> errDataIntegrityLeak(res, spec);
                case "ERR-BADREQUEST-ECHO" -> errBadRequestEcho(res, spec);
                case "ERR-CLIENT-ABORT-ENVELOPE" -> errClientAbortEnvelope(res, spec);
                case "ERR-UNKNOWN-NO-LEAK" -> errUnknownNoLeak(res, spec);
                case "ADVICE-ENVELOPE" -> adviceEnvelope(res, spec);
                default -> res.put("error", "unknown scenario " + id);
            }
        } catch (Throwable t) {
            res.put("escaped_exception", t.getClass().getName());
            res.put("escaped_message", String.valueOf(t.getMessage()));
        }
        res.set("repo_calls", M.valueToTree(CALL_LOG));
        res.set("logs", M.valueToTree(capturedLogs()));
        return res;
    }

    // -------------------------------------------------------------- observation helpers

    static ArrayNode itemsOf(BanquetPackage pkg) {
        ArrayNode arr = M.createArrayNode();
        if (pkg == null || pkg.getItems() == null) return arr;
        for (PackageItem it : pkg.getItems()) {
            ObjectNode o = M.createObjectNode();
            o.put("itemId", it.getId());
            o.put("productId", it.getProduct() == null ? null : it.getProduct().getId());
            o.put("quantity", it.getQuantity());
            arr.add(o);
        }
        return arr;
    }

    static void recordError(ObjectNode res, Throwable t) {
        if (t instanceof BusinessException be) {
            res.put("error_type", "BusinessException");
            res.put("error_code", be.getErrorCode().name());
            res.put("http_status", be.getStatus().value());
            res.put("error_message", be.getMessage());
        } else {
            res.put("error_type", t.getClass().getName());
            res.put("error_message", String.valueOf(t.getMessage()));
            res.put("is_business_exception", false);
        }
    }

    static List<PackageItemRequest> items(long... productIds) {
        List<PackageItemRequest> list = new ArrayList<>();
        for (long pid : productIds) {
            list.add(new PackageItemRequest(pid, 2));
        }
        return list;
    }

    // ------------------------------------------------------------------- scenarios

    static void pkgUpdateSubset(ObjectNode res, JsonNode spec) {
        BanquetPackage pkg = packageWithItems(4L, "双人精选宴", 46800L, 11L, 13L);
        res.set("before_items", itemsOf(pkg));
        PackageService svc = new PackageService(packageRepo(pkg), productRepo(Set.of()), typeRepo(Set.of("婚宴")));
        UpdatePackageRequest req = new UpdatePackageRequest("双人精选宴", "婚宴", 46800L, null, null, null, items(11L));
        try {
            var resp = svc.update(4L, req);
            res.put("result", "ok");
            res.put("response_item_count", resp.items().size());
        } catch (Throwable t) {
            res.put("result", "threw");
            recordError(res, t);
        }
        res.set("after_items", itemsOf(pkg));
    }

    static void pkgUpdateMissingProduct(ObjectNode res, JsonNode spec) {
        BanquetPackage pkg = packageWithItems(4L, "双人精选宴", 46800L, 11L, 13L);
        res.set("before_items", itemsOf(pkg));
        // 999999 不存在：applyItems 里 removeIf 先执行，findProduct 后抛 404
        PackageService svc = new PackageService(packageRepo(pkg), productRepo(Set.of(999999L)), typeRepo(Set.of("婚宴")));
        UpdatePackageRequest req = new UpdatePackageRequest("双人精选宴", "婚宴", 46800L, null, null, null, items(999999L));
        try {
            svc.update(4L, req);
            res.put("result", "ok");
        } catch (Throwable t) {
            res.put("result", "threw");
            recordError(res, t);
        }
        res.set("after_items", itemsOf(pkg));
        res.put("entity_name_after", pkg.getName());
        res.put("entity_price_after", pkg.getPriceCent());
    }

    static void pkgCreateMissingProduct(ObjectNode res, JsonNode spec) {
        PackageService svc = new PackageService(packageRepo(null), productRepo(Set.of(999999L)), typeRepo(Set.of("婚宴")));
        CreatePackageRequest req = new CreatePackageRequest("新套餐", "婚宴", 1000L, null, null, null, items(999999L));
        try {
            svc.create(req);
            res.put("result", "ok");
        } catch (Throwable t) {
            res.put("result", "threw");
            recordError(res, t);
        }
    }

    static void pkgNullBanquetType(ObjectNode res, JsonNode spec) {
        BanquetPackage pkg = packageWithItems(4L, "双人精选宴", 46800L, 11L);
        PackageService svc = new PackageService(packageRepo(pkg), productRepo(Set.of()), typeRepo(Set.of("婚宴")));
        UpdatePackageRequest req = new UpdatePackageRequest("双人精选宴", null, 46800L, null, null, null, items(11L));
        try {
            svc.update(4L, req);
            res.put("result", "ok");
        } catch (Throwable t) {
            res.put("result", "threw");
            recordError(res, t);
        }
        res.set("after_items", itemsOf(pkg));
    }

    static void pkgEmptyItems(ObjectNode res, JsonNode spec) {
        BanquetPackage pkg = packageWithItems(4L, "双人精选宴", 46800L, 11L);
        PackageService svc = new PackageService(packageRepo(pkg), productRepo(Set.of()), typeRepo(Set.of("婚宴")));
        UpdatePackageRequest req = new UpdatePackageRequest("双人精选宴", "婚宴", 46800L, null, null, null, List.of());
        try {
            svc.update(4L, req);
            res.put("result", "ok");
        } catch (Throwable t) {
            res.put("result", "threw");
            recordError(res, t);
        }
    }

    static void pkgDuplicateItems(ObjectNode res, JsonNode spec) {
        BanquetPackage pkg = packageWithItems(4L, "双人精选宴", 46800L, 11L);
        PackageService svc = new PackageService(packageRepo(pkg), productRepo(Set.of()), typeRepo(Set.of("婚宴")));
        UpdatePackageRequest req = new UpdatePackageRequest("双人精选宴", "婚宴", 46800L, null, null, null, items(11L, 11L));
        try {
            svc.update(4L, req);
            res.put("result", "ok");
        } catch (Throwable t) {
            res.put("result", "threw");
            recordError(res, t);
        }
    }

    static void pkgQuantityZero(ObjectNode res, JsonNode spec) {
        BanquetPackage pkg = packageWithItems(4L, "双人精选宴", 46800L, 11L);
        PackageService svc = new PackageService(packageRepo(pkg), productRepo(Set.of()), typeRepo(Set.of("婚宴")));
        List<PackageItemRequest> reqs = new ArrayList<>();
        reqs.add(new PackageItemRequest(11L, 0));
        UpdatePackageRequest req = new UpdatePackageRequest("双人精选宴", "婚宴", 46800L, null, null, null, reqs);
        try {
            svc.update(4L, req);
            res.put("result", "ok");
        } catch (Throwable t) {
            res.put("result", "threw");
            recordError(res, t);
        }
        res.set("after_items", itemsOf(pkg));
    }

    static void pkgDupDbItems(ObjectNode res, JsonNode spec) {
        // 库里若出现同套餐同商品的两条明细，Collectors.toMap 会抛重复键
        BanquetPackage pkg = packageWithItems(4L, "双人精选宴", 46800L, 11L);
        PackageItem dup = new PackageItem();
        dup.setId(9999L);
        dup.setBanquetPackage(pkg);
        dup.setProduct(PRODUCTS.get(11L));
        dup.setQuantity(5);
        pkg.getItems().add(dup);
        PackageService svc = new PackageService(packageRepo(pkg), productRepo(Set.of()), typeRepo(Set.of("婚宴")));
        UpdatePackageRequest req = new UpdatePackageRequest("双人精选宴", "婚宴", 46800L, null, null, null, items(11L));
        try {
            svc.update(4L, req);
            res.put("result", "ok");
        } catch (Throwable t) {
            res.put("result", "threw");
            recordError(res, t);
        }
    }

    static void pkgUpdateMissingPackage(ObjectNode res, JsonNode spec) {
        PackageService svc = new PackageService(packageRepo(null), productRepo(Set.of()), typeRepo(Set.of("婚宴")));
        UpdatePackageRequest req = new UpdatePackageRequest("x", "婚宴", 1L, null, null, null, items(11L));
        try {
            svc.update(404L, req);
            res.put("result", "ok");
        } catch (Throwable t) {
            res.put("result", "threw");
            recordError(res, t);
        }
    }

    // ------------------------------------------------------------------- CORS / 静态资源

    static final String DEFAULT_ORIGINS =
        "http://localhost:[*],http://127.0.0.1:[*],http://192.168.*.*:[*],http://10.*.*.*:[*],http://172.*.*.*:[*]";

    /** CorsRegistry.getCorsConfigurations() 是 protected，只能从子类里拿。 */
    static class InspectableCorsRegistry extends CorsRegistry {
        Map<String, CorsConfiguration> configs() {
            return getCorsConfigurations();
        }
    }

    static void corsOriginMatrix(ObjectNode res, JsonNode spec) {
        String origins = spec.path("origins").asText(DEFAULT_ORIGINS);
        WebConfig cfg = new WebConfig(origins, "/tmp/jiuyan_file/uploads");
        InspectableCorsRegistry registry = new InspectableCorsRegistry();
        cfg.addCorsMappings(registry);
        Map<String, CorsConfiguration> configs = registry.configs();
        res.put("mapping_keys", M.valueToTree(configs.keySet()));
        CorsConfiguration c = configs.get("/api/**");
        if (c == null) {
            res.put("config_found", false);
            return;
        }
        res.put("config_found", true);
        res.set("allowed_origin_patterns", M.valueToTree(c.getAllowedOriginPatterns()));
        ArrayNode probes = M.createArrayNode();
        for (String origin : new String[]{
                "http://localhost:5173", "http://localhost:9999", "http://127.0.0.1:5173",
                "http://192.168.1.5:8080", "http://192.168.1.5:9999", "http://10.0.0.7:3000",
                "http://172.16.0.9:1234", "http://evil.example.com", "https://evil.example.com",
                "http://localhost.evil.com", "null", "http://[::1]:5173"}) {
            ObjectNode p = M.createObjectNode();
            p.put("origin", origin);
            p.put("allowed", c.checkOrigin(origin) != null);
            probes.add(p);
        }
        res.set("probes", probes);
    }

    static void corsCredentials(ObjectNode res, JsonNode spec) {
        WebConfig cfg = new WebConfig(DEFAULT_ORIGINS, "/tmp/jiuyan_file/uploads");
        InspectableCorsRegistry registry = new InspectableCorsRegistry();
        cfg.addCorsMappings(registry);
        CorsConfiguration c = registry.configs().get("/api/**");
        res.put("allow_credentials", c != null && Boolean.TRUE.equals(c.getAllowCredentials()));
        res.set("allowed_methods", M.valueToTree(c == null ? List.of() : c.getAllowedMethods()));
        res.set("allowed_headers", M.valueToTree(c == null ? List.of() : c.getAllowedHeaders()));
        res.set("exposed_headers", M.valueToTree(c == null ? List.of() : c.getExposedHeaders()));
    }

    static void uploadsResourceLocation(ObjectNode res, JsonNode spec) throws Exception {
        Path base = Paths.get("/tmp/jiuyan_file/loc-test");
        Path existing = base.resolve("exists");
        Path missing = base.resolve("missing");
        Files.createDirectories(existing);
        Files.deleteIfExists(missing);
        Files.createDirectories(missing);
        Files.deleteIfExists(missing);   // 故意让它不存在
        res.put("existing_dir_to_uri", Path.of(existing.toString()).toAbsolutePath().normalize().toUri().toString());
        res.put("missing_dir_to_uri", Path.of(missing.toString()).toAbsolutePath().normalize().toUri().toString());
        // Spring 静态映射如何拼接相对路径：带尾斜杠才对
        Resource locSlash = new UrlResource(ResourceUtils.getURL("file://" + existing.toAbsolutePath() + "/"));
        Resource locNoSlash = new UrlResource(ResourceUtils.getURL("file://" + missing.toAbsolutePath()));
        res.put("relative_with_slash", locSlash.createRelative("seed/banquet-table.png").getURL().toString());
        res.put("relative_without_slash", locNoSlash.createRelative("seed/banquet-table.png").getURL().toString());
        // WebConfig 真正交给 Spring 的 location 字符串
        res.put("webconfig_location_for_existing", Path.of(existing.toString()).toAbsolutePath().normalize().toUri().toString());
        res.put("webconfig_location_for_missing", Path.of(missing.toString()).toAbsolutePath().normalize().toUri().toString());
    }

    static void uploadsSameRoot(ObjectNode res, JsonNode spec) {
        String uploadDir = spec.path("upload_dir").asText("/app/uploads");
        UploadService svc = new UploadService(uploadDir);
        Path webRoot = Path.of(uploadDir).toAbsolutePath().normalize();
        Path svcRoot = readUploadRoot(svc);
        res.put("webconfig_root", webRoot.toString());
        res.put("uploadservice_root", svcRoot == null ? null : svcRoot.toString());
        res.put("same_root", svcRoot != null && svcRoot.equals(webRoot));
        res.put("webconfig_uri", webRoot.toUri().toString());
    }

    static Path readUploadRoot(UploadService svc) {
        try {
            java.lang.reflect.Field f = UploadService.class.getDeclaredField("uploadRoot");
            f.setAccessible(true);
            return (Path) f.get(svc);
        } catch (ReflectiveOperationException e) {
            return null;
        }
    }

    // ------------------------------------------------------------------- TraceIdFilter

    static void traceXffSpoof(ObjectNode res, JsonNode spec) throws Exception {
        MockHttpServletRequest req = new MockHttpServletRequest("GET", "/api/products");
        req.setRemoteAddr("127.0.0.1");
        req.addHeader("X-Forwarded-For", "9.9.9.9, 10.0.0.1");
        MockHttpServletResponse resp = new MockHttpServletResponse();
        TraceIdFilter filter = new TraceIdFilter();
        filter.doFilter(req, resp, new MockFilterChain());
        res.put("remote_addr", req.getRemoteAddr());
        res.put("xff_header", req.getHeader("X-Forwarded-For"));
        res.set("logs", M.valueToTree(capturedLogs()));
        res.put("logged_client_ip", extractField(capturedLogs(), "clientIp="));
    }

    static void traceIdVerbatim(ObjectNode res, JsonNode spec) throws Exception {
        String injected = spec.path("trace_id").asText("FILELAYER-TRACE-77");
        MockHttpServletRequest req = new MockHttpServletRequest("GET", "/api/products");
        req.setRemoteAddr("127.0.0.1");
        req.addHeader("X-Trace-Id", injected);
        MockHttpServletResponse resp = new MockHttpServletResponse();
        TraceIdFilter filter = new TraceIdFilter();
        filter.doFilter(req, resp, new MockFilterChain());
        res.put("sent_trace_id", injected);
        res.put("response_trace_id", resp.getHeader("X-Trace-Id"));
        res.put("trace_context_value", TraceContext.getTraceId());
        res.set("logs", M.valueToTree(capturedLogs()));
        res.put("log_contains_raw_trace_id", capturedLogs().stream().anyMatch(l -> l.contains(injected)));
    }

    static void traceMdcCleanup(ObjectNode res, JsonNode spec) throws Exception {
        MockHttpServletRequest req = new MockHttpServletRequest("GET", "/api/products");
        MockHttpServletResponse resp = new MockHttpServletResponse();
        TraceIdFilter filter = new TraceIdFilter();
        filter.doFilter(req, resp, new MockFilterChain());
        res.put("mdc_after", org.slf4j.MDC.get("traceId"));
        res.put("trace_context_after", TraceContext.getTraceId());
        res.put("response_header_present", resp.getHeader("X-Trace-Id") != null);
    }

    static String extractField(List<String> logs, String prefix) {
        for (String l : logs) {
            int i = l.indexOf(prefix);
            if (i >= 0) {
                String rest = l.substring(i + prefix.length());
                int sp = rest.indexOf(' ');
                return sp > 0 ? rest.substring(0, sp) : rest;
            }
        }
        return null;
    }

    // ------------------------------------------------------------- 异常处理 / 信封

    static void errDataIntegrityLeak(ObjectNode res, JsonNode spec) throws Exception {
        String dbMessage = spec.path("db_message").asText(
            "could not execute statement [ERROR: duplicate key value violates unique constraint \"uk_orders_order_no\"]");
        DataIntegrityViolationException ex = new DataIntegrityViolationException(
            "could not execute statement", new IllegalStateException(dbMessage));
        MockHttpServletRequest req = new MockHttpServletRequest("POST", "/api/orders");
        GlobalExceptionHandler h = new GlobalExceptionHandler();
        var response = h.handleDataIntegrity(ex, req);
        res.put("http_status", response.getStatusCode().value());
        String body = M.writeValueAsString(response.getBody());
        res.put("body", body);
        res.put("body_contains_constraint_name", body.contains("uk_orders_order_no"));
        res.put("body_contains_db_vendor_detail", body.contains("could not execute statement"));
    }

    static void errBadRequestEcho(ObjectNode res, JsonNode spec) throws Exception {
        String raw = spec.path("raw_message").asText(
            "JSON parse error: Unexpected character ('s' (code 115)) in JSON string: {\"password\":\"hunter2\"} at [Source: (String)\"; line: 1, column: 20]");
        HttpMessageNotReadableException ex = new HttpMessageNotReadableException(raw, (org.springframework.http.HttpInputMessage) null);
        MockHttpServletRequest req = new MockHttpServletRequest("POST", "/api/orders");
        GlobalExceptionHandler h = new GlobalExceptionHandler();
        var response = h.handleBadRequest(ex, req);
        res.put("http_status", response.getStatusCode().value());
        String body = M.writeValueAsString(response.getBody());
        res.put("body", body);
        res.put("body_contains_raw_fragment", body.contains("hunter2"));
    }

    static void errClientAbortEnvelope(ObjectNode res, JsonNode spec) throws Exception {
        MockHttpServletRequest req = new MockHttpServletRequest("POST", "/api/orders");
        GlobalExceptionHandler h = new GlobalExceptionHandler();
        var ex = new org.springframework.web.context.request.async.AsyncRequestNotUsableException("broken pipe");
        var response = h.handleClientAbort(ex, req);
        res.put("http_status", response.getStatusCode().value());
        res.put("body_is_null", response.getBody() == null);
        res.put("has_envelope_fields", response.getBody() != null
            && response.getBody().getClass().getSimpleName().contains("ApiResponse"));
    }

    static void errUnknownNoLeak(ObjectNode res, JsonNode spec) throws Exception {
        MockHttpServletRequest req = new MockHttpServletRequest("GET", "/api/products");
        GlobalExceptionHandler h = new GlobalExceptionHandler();
        var ex = new IllegalStateException("com.zaxxer.hikari.pool.HikariPool : connection is not available");
        var response = h.handleUnknown(ex, req);
        res.put("http_status", response.getStatusCode().value());
        String body = M.writeValueAsString(response.getBody());
        res.put("body", body);
        res.put("body_contains_internal_detail", body.contains("HikariPool"));
    }

    public static class AdviceProbe {
        public Product plain() { return null; }
        public ApiResponse<Object> enveloped() { return null; }
        public ResponseEntity<ApiResponse<Object>> entity() { return null; }
    }

    static void adviceEnvelope(ObjectNode res, JsonNode spec) throws Exception {
        ApiResponseAdvice advice = new ApiResponseAdvice();
        Class<? extends org.springframework.http.converter.HttpMessageConverter<?>> converter = MappingJackson2HttpMessageConverter.class;
        ObjectNode supports = M.createObjectNode();
        for (String m : new String[]{"plain", "enveloped", "entity"}) {
            try {
                Method method = AdviceProbe.class.getMethod(m);
                MethodParameter p = new MethodParameter(method, -1);
                supports.put(m, advice.supports(p, converter));
            } catch (NoSuchMethodException e) {
                supports.put(m, "n/a");
            }
        }
        res.set("supports", supports);

        MockHttpServletRequest servletReq = new MockHttpServletRequest("GET", "/api/products");
        MockHttpServletResponse servletResp = new MockHttpServletResponse();
        ServletServerHttpRequest req = new ServletServerHttpRequest(servletReq);
        ServletServerHttpResponse resp = new ServletServerHttpResponse(servletResp);
        MethodParameter plainParam = new MethodParameter(AdviceProbe.class.getMethod("plain"), -1);
        TraceContext.setTraceId("FILELAYER-CONTEXT-TRACE");
        res.put("trace_context_set", TraceContext.getTraceId());

        Object okBody = advice.beforeBodyWrite(new java.util.HashMap<>(Map.of("id", 1)), plainParam,
            MediaType.APPLICATION_JSON, converter, req, resp);
        res.put("success_envelope_json", M.writeValueAsString(okBody));

        Object nullBody = advice.beforeBodyWrite(null, plainParam,
            MediaType.APPLICATION_JSON, converter, req, resp);
        res.put("null_body_result_is_null", nullBody == null);
        res.put("null_body_json", nullBody == null ? null : M.writeValueAsString(nullBody));

        Object alreadyWrapped = advice.beforeBodyWrite(
            ApiResponse.error("X", "msg", null, "t", java.time.OffsetDateTime.now()), plainParam,
            MediaType.APPLICATION_JSON, converter, req, resp);
        res.put("already_envelope_unchanged", alreadyWrapped instanceof ApiResponse);
    }
}
