import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.lang.reflect.Array;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.lang.reflect.Proxy;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 文件层执行载体（通用版）：在同一个 JVM 内整类实例化，仓库打桩，抓日志，看中间态。
 *
 * 与函数层的区别：函数层看「一个方法算得对不对」，文件层看「一个文件里的方法编排
 * 顺不对、失败之后实体被改成什么样、资源边界是不是同一个」。
 *
 * 本文件不含任何项目字面量 —— 被测项目名、类名、路径、配置项全部来自用例文件。
 * 换项目只换用例文件，不改编本文件。
 *
 * 用例文件结构：
 * {
 *   "fixtures": {"pkg4": {"$entity": "com.x.Package", "fields": {...}}},
 *   "stubs":    {"com.x.YRepo": {"method": <值规格>}},
 *   "stub_labels": {"com.x.YRepo": "yRepo"},
 *   "ctor":     {"com.x.ZService": {"0": <值规格>}},
 *   "views":    {"pkg_items": {"source": "items", "list": true, "fields": {...}}},
 *   "scenarios": [ {"id": "...", "class": "...", "steps": [...], "measures": [...]} ],
 *   "detectors": {"enable": [...], "args": {...}},
 *   "log_package": "com.x"
 * }
 */
public class GenericFileHarness extends GenericFileHarnessObserve {
    // ---- 方法 ----
    public static void main(String[] args) throws Exception {
        WORK_DIR = Paths.get(args.length > 2 ? args[2] : System.getProperty("java.io.tmpdir"));
        CLASSES_ROOT = args.length > 3 && !args[3].isEmpty() ? Paths.get(args[3]) : null;
        ROOT = M.readTree(Paths.get(args[0]).toFile());
        STUBS = ROOT.path("stubs");
        FIXTURES = ROOT.path("fixtures");
        CTOR = ROOT.path("ctor");
        VIEWS = ROOT.path("views");
        var labels = ROOT.path("stub_labels").fields();
        while (labels.hasNext()) {
            var e = labels.next();
            STUB_LABELS.put(e.getKey(), e.getValue().asText());
        }
        attachLogCapture(ROOT.path("log_package").asText(""));

        ObjectNode out = M.createObjectNode();
        ArrayNode scenarios = M.createArrayNode();
        for (JsonNode c : ROOT.path("scenarios")) {
            scenarios.add(runScenario(c));
        }
        out.set("scenarios", scenarios);
        out.set("detectors", runDetectors());
        Files.writeString(Paths.get(args[1]), out.toString());
    }

    static ObjectNode runScenario(JsonNode spec) {
        ObjectNode res = M.createObjectNode();
        res.put("id", spec.path("id").asText());
        CALL_LOG.clear();
        CALL_COUNTS.clear();
        CAPTURED.clear();
        LAST_ARGS.clear();
        LAST_RETURN = null;
        LAST_RESPONSE = null;
        FIXTURE_OBJECTS.clear();
        CASE_STUBS = spec.path("stubs");
        CASE_FIXTURES = spec.path("fixtures");
        CASE_CTOR = spec.path("ctor");
        clearLogs();
        try {
            String cls = spec.path("class").asText("");
            TARGET = cls.isEmpty() ? null : newTarget(cls);
            for (JsonNode step : spec.path("steps")) {
                runStep(step, res);
            }
            for (JsonNode meas : spec.path("measures")) {
                String name = meas.path("name").asText("");
                res.set(name, measure(meas, res));
            }
        } catch (Throwable t) {
            res.put("escaped_exception", t.getClass().getName());
            res.put("escaped_message", String.valueOf(t.getMessage()));
        }
        res.set("repo_calls", M.valueToTree(CALL_LOG));
        res.set("logs", M.valueToTree(capturedLogs()));
        ObjectNode counts = M.createObjectNode();
        CALL_COUNTS.forEach((k, v) -> counts.put(k, v.get()));
        res.set("call_counts", counts);
        return res;
    }

    static void runStep(JsonNode step, ObjectNode res) throws Exception {
        // 任何字段都可以引用模板（{"$t":"template","v":"名字"}）—— 模板只做减法，不含项目语义
        step = expandTemplates(step);
        String op = step.path("op").asText("");
        switch (op) {
            case "call" -> doCall(step, res);
            case "observe" -> doObserve(step, res);
            case "read" -> doRead(step, res);
            case "field" -> doField(step, res);
            case "invoke_on" -> doInvokeOn(step, res);
            case "cors_probe" -> doCorsProbe(step, res);
            case "path_facts" -> doPathFacts(step, res);
            case "static_call" -> doStaticCall(step, res);
            case "mdc_get" -> doMdcGet(step, res);
            case "response_header" -> doResponseHeader(step, res);
            case "log_field" -> doLogField(step, res);
            case "const" -> doConst(step, res);
            case "instantiate" -> doInstantiate(step, res);
            case "call_on" -> doCallOn(step, res);
            case "advice_supports" -> doAdviceSupports(step, res);
            case "path_fact" -> doPathFact(step, res);
            case "handler" -> doHandler(step, res);
            default -> res.put("step_unsupported", op);
        }
    }

    /** 方法调用：实例方法或静态方法。抛异常不中断场景（后续步骤继续跑，看中间态）。 */
    static void doCall(JsonNode step, ObjectNode res) throws Exception {
        String cls = step.path("class").asText("");
        Method method;
        Object receiver = null;
        if (!cls.isEmpty()) {
            Class<?> k = Class.forName(cls);
            method = pickMethod(allMethods(k, step.path("method").asText()),
                    step.path("args").size());
            if (!step.path("static").asBoolean(false)) {
                receiver = newInstanceOf(k);
            }
        } else {
            receiver = TARGET;
            if (receiver == null) {
                throw new IllegalStateException("场景没有目标实例");
            }
            method = pickMethod(allMethods(receiver.getClass(), step.path("method").asText()),
                    step.path("args").size());
        }
        if (method == null) {
            throw new NoSuchMethodException("找不到方法 " + step.path("method").asText());
        }
        method.setAccessible(true);
        Class<?>[] types = method.getParameterTypes();
        Object[] argv = new Object[types.length];
        JsonNode args = step.path("args");
        for (int i = 0; i < types.length; i++) {
            argv[i] = i < args.size() ? decode(args.get(i), types[i]) : autoArg(types[i], step);
            LAST_ARGS.put(i, argv[i]);
        }
        boolean recordOutcome = step.path("outcome").asBoolean(true);
        String outcomeKey = step.path("outcome_as").asText("result");
        Object ret;
        try {
            ret = method.invoke(receiver, argv);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause() == null ? e : e.getCause();
            if (recordOutcome) {
                res.put(outcomeKey, "threw");
            }
            recordError(res, cause);
            LAST_RETURN = null;
            return;
        }
        LAST_RETURN = ret;
        if (recordOutcome) {
            res.put(outcomeKey, "ok");
        }
        JsonNode capture = step.path("capture");
        var it = capture.fields();
        while (it.hasNext()) {
            var e = it.next();
            res.set(e.getKey(), captureValue(e.getValue(), ret));
        }
    }

    static void doObserve(JsonNode step, ObjectNode res) {
        Object subject = subject(step.path("subject").asText("target"));
        String view = step.path("view").asText("");
        res.set(step.path("as").asText(view), applyView(view, subject));
    }

    static void doRead(JsonNode step, ObjectNode res) {
        Object subject = subject(step.path("subject").asText("target"));
        Object v = readPath(subject, step.path("path").asText(""));
        if ("text".equals(step.path("mode").asText(""))) {
            res.put(step.path("as").asText(""), String.valueOf(v));
        } else {
            res.set(step.path("as").asText(""), toJson(v));
        }
    }

    static void doField(JsonNode step, ObjectNode res) throws Exception {
        Object subject = subject(step.path("subject").asText("target"));
        String name = step.path("field").asText("");
        Field f = findField(subject.getClass(), name);
        if (f == null) {
            res.putNull(step.path("as").asText(name));
            return;
        }
        f.setAccessible(true);
        Object v = f.get(subject);
        if ("text".equals(step.path("mode").asText(""))) {
            res.put(step.path("as").asText(name), String.valueOf(v));
        } else {
            res.set(step.path("as").asText(name), toJson(v));
        }
    }

    /** 在某个对象上再调一个方法（protected 的也能调），结果留在 CAPTURED 里。 */
    static void doInvokeOn(JsonNode step, ObjectNode res) throws Exception {
        Object subject = subject(step.path("subject").asText("target"));
        Method m = findDeclared(subject.getClass(), step.path("method").asText());
        if (m == null) {
            res.putNull(step.path("as").asText(""));
            return;
        }
        m.setAccessible(true);
        Object v = m.invoke(subject);
        String name = step.path("as").asText("");
        CAPTURED.put(name, v);
        if (step.path("record").asBoolean(false)) {
            res.set(name, toJson(v));
        }
    }

    static void doStaticCall(JsonNode step, ObjectNode res) throws Exception {
        Class<?> k = Class.forName(step.path("class").asText());
        Method m = pickMethod(allMethods(k, step.path("method").asText()), step.path("args").size());
        m.setAccessible(true);
        Class<?>[] types = m.getParameterTypes();
        Object[] argv = new Object[types.length];
        JsonNode args = step.path("args");
        for (int i = 0; i < types.length; i++) {
            argv[i] = i < args.size() ? decode(args.get(i), types[i]) : autoArg(types[i], step);
        }
        Object v = m.invoke(null, argv);
        String as = step.path("as").asText("");
        if (!as.isEmpty()) {
            res.set(as, toJson(v));
        }
    }

    static void doMdcGet(JsonNode step, ObjectNode res) {
        String key = step.path("key").asText("");
        Object v = null;
        try {
            Class<?> mdc = Class.forName("org.slf4j.MDC");
            v = mdc.getMethod("get", String.class).invoke(null, key);
        } catch (Exception e) {
            v = null;
        }
        res.set(step.path("as").asText("mdc"), toJson(v));
    }

    static void doResponseHeader(JsonNode step, ObjectNode res) {
        String name = step.path("name").asText("");
        Object v = null;
        if (LAST_RESPONSE != null) {
            try {
                Method m = LAST_RESPONSE.getClass().getMethod("getHeader", String.class);
                v = m.invoke(LAST_RESPONSE, name);
            } catch (Exception ignored) {
                v = null;
            }
        }
        String as = step.path("as").asText(name);
        if (as.isEmpty()) {
            return;
        }
        if ("present".equals(step.path("mode").asText("value"))) {
            res.put(as, v != null);
        } else {
            res.set(as, toJson(v));
        }
    }

    static void doLogField(JsonNode step, ObjectNode res) {
        String prefix = step.path("prefix").asText("");
        Object v = null;
        for (String line : capturedLogs()) {
            int i = line.indexOf(prefix);
            if (i >= 0) {
                String rest = line.substring(i + prefix.length());
                int sp = rest.indexOf(' ');
                v = sp > 0 ? rest.substring(0, sp) : rest;
                break;
            }
        }
        res.set(step.path("as").asText("log_field"), toJson(v));
    }

    /**
     * CORS 白名单矩阵：读出配置好的 CORS 配置，对一批 Origin 逐个问「允不允许」。
     * 只用 Spring 的公开语义（checkOrigin），不碰任何项目类。
     */
    static void doCorsProbe(JsonNode step, ObjectNode res) throws Exception {
        Map<?, ?> map = corsMap(CAPTURED.get(step.path("from").asText("")));
        String pattern = step.path("pattern").asText("");
        Object cfg = map.get(pattern);
        JsonNode as = step.path("as");
        var names = as.fields();
        while (names.hasNext()) {
            var e = names.next();
            String key = e.getKey();
            String raw = e.getValue().asText(key);
            Object value;
            switch (key) {
                case "mapping_keys" -> value = new ArrayList<>(map.keySet());
                case "config_found" -> value = cfg != null;
                case "patterns" -> value = callNoArg(cfg, "getAllowedOriginPatterns");
                case "credentials" -> value = callNoArg(cfg, "getAllowCredentials");
                case "methods" -> value = callNoArg(cfg, "getAllowedMethods");
                case "headers" -> value = callNoArg(cfg, "getAllowedHeaders");
                case "exposed" -> value = callNoArg(cfg, "getExposedHeaders");
                case "probes" -> value = corsProbes(cfg, step.path("origins"));
                default -> value = callNoArg(cfg, key);
            }
            res.set(raw, toJson(value));
        }
    }

    /**
     * 路径语义事实：目录存在与否时 toUri() 的尾斜杠、Spring 相对路径拼接结果。
     * 纯文件系统语义，与项目无关；用完即删（零残留）。
     *
     * dirs  : 先把目录准备好（存在 / 不存在），顺序在这里定死
     * probes: 再逐个取事实，避免探针顺序反过来影响目录状态
     */
    static void doPathFacts(JsonNode step, ObjectNode res) throws Exception {
        Path base = Paths.get(step.path("base").asText(""));
        Files.createDirectories(base);
        for (JsonNode d : step.path("dirs")) {
            Path dir = base.resolve(d.path("name").asText("probe"));
            if (d.path("create").asBoolean(false)) {
                Files.createDirectories(dir);
            } else {
                Files.deleteIfExists(dir);
            }
        }
        for (JsonNode probe : step.path("probes")) {
            Path dir = base.resolve(probe.path("dir").asText("probe"));
            String uri = dir.toAbsolutePath().normalize().toUri().toString();
            Object value = uri;
            String relative = probe.path("relative").asText("");
            if (!relative.isEmpty()) {
                value = resolveRelative(uri, relative);
            } else if ("resource_location".equals(probe.path("via").asText(""))) {
                // 走配置类自己的注册逻辑（addResourceHandlers），拿它真正注册进去的位置
                value = resourceLocation(probe);
            }
            res.set(probe.path("name").asText(dir.toString()), toJson(value));
        }
        try (var stream = Files.walk(base)) {
            stream.sorted((a, b) -> b.getNameCount() - a.getNameCount()).forEach(p -> {
                try {
                    Files.deleteIfExists(p);
                } catch (IOException ignored) {
                    // 忽略
                }
            });
        }
    }

    /**
     * 异常处理器直调：拿 ResponseEntity 的状态码与响应体。
     * 只用公开语义（getStatusCode / getBody），项目类名全部由用例文件给出。
     */
    static void doHandler(JsonNode step, ObjectNode res) throws Exception {
        Class<?> k = Class.forName(step.path("class").asText());
        Object target = k.getDeclaredConstructor().newInstance();
        Method m = pickMethod(allMethods(k, step.path("method").asText()), step.path("args").size());
        if (m == null) {
            throw new NoSuchMethodException("找不到方法 " + step.path("method").asText());
        }
        m.setAccessible(true);
        Class<?>[] types = m.getParameterTypes();
        Object[] argv = new Object[types.length];
        JsonNode args = step.path("args");
        for (int i = 0; i < types.length; i++) {
            argv[i] = i < args.size() ? decode(args.get(i), types[i]) : autoArg(types[i], step);
        }
        Object ret = m.invoke(target, argv);
        Object body = callNoArg(ret, "getBody");
        String statusAs = step.path("status_as").asText("http_status");
        if (!statusAs.isEmpty()) {
            Object status = readPath(ret, "statusCode.value");
            res.set(statusAs, status == null
                    ? M.getNodeFactory().nullNode()
                    : M.getNodeFactory().numberNode(((Number) status).intValue()));
        }
        String bodyAs = step.path("body_as").asText("");
        if (!bodyAs.isEmpty()) {
            res.set(bodyAs, body == null
                    ? M.getNodeFactory().nullNode()
                    : M.getNodeFactory().textNode(M.writeValueAsString(body)));
        }
        String nullAs = step.path("body_null_as").asText("");
        if (!nullAs.isEmpty()) {
            res.set(nullAs, M.getNodeFactory().booleanNode(body == null));
        }
        String envAs = step.path("envelope_as").asText("");
        if (!envAs.isEmpty()) {
            String envClass = step.path("envelope_class").asText("");
            res.set(envAs, M.getNodeFactory().booleanNode(
                    body != null && !envClass.isEmpty() && envClass.equals(body.getClass().getName())));
        }
    }

    /** 用用例文件的构造规格造实例；没给就用无参构造。 */
    static Object newInstanceOf(Class<?> k) throws Exception {
        try {
            Constructor<?> c = k.getDeclaredConstructor();
            c.setAccessible(true);
            return c.newInstance();
        } catch (NoSuchMethodException e) {
            return newTarget(k.getName());
        }
    }

    /** 用步骤自带的构造参数造实例（没给就用用例文件的 ctor 规格）。 */
    static Object newInstanceWith(JsonNode step, Class<?> k) throws Exception {
        JsonNode args = step.path("ctor_args");
        if (args.isMissingNode() || args.isEmpty()) {
            return newInstanceOf(k);
        }
        Constructor<?> best = null;
        for (Constructor<?> c : k.getDeclaredConstructors()) {
            if (best == null || c.getParameterCount() < best.getParameterCount()) {
                best = c;
            }
        }
        Class<?>[] pt = best.getParameterTypes();
        Object[] vals = new Object[pt.length];
        for (int i = 0; i < pt.length; i++) {
            JsonNode spec = args.path(String.valueOf(i));
            vals[i] = spec.isMissingNode() ? defaultValue(pt[i]) : buildValue(spec, pt[i], new Object[0]);
        }
        best.setAccessible(true);
        return best.newInstance(vals);
    }

    /**
     * 静态资源映射位置：调配置类的 addResourceHandlers(registry)，读回它真正注册进去的位置。
     * 用例文件给类名和构造参数 —— 通用 harness 不认识任何项目类。
     */
    static Object resourceLocation(JsonNode probe) throws Exception {
        Class<?> k = Class.forName(probe.path("class").asText());
        Object cfg = newInstanceWith(probe, k);
        Class<?> registryClass = Class.forName(
                "org.springframework.web.servlet.config.annotation.ResourceHandlerRegistry");
        Constructor<?> rc = null;
        for (Constructor<?> c : registryClass.getDeclaredConstructors()) {
            if (c.getParameterCount() == 2) {
                rc = c;
                break;
            }
        }
        rc.setAccessible(true);
        Object registry = rc.newInstance(null, null);
        Method add = pickMethod(allMethods(k, "addResourceHandlers"), 1);
        add.setAccessible(true);
        add.invoke(cfg, registry);
        return firstResourceLocation(registry);
    }

    static Object firstResourceLocation(Object registry) {
        Method m = findMethodAny(registry.getClass(), "getHandlerMapping", 0);
        if (m == null) {
            return null;
        }
        m.setAccessible(true);
        Object mapping;
        try {
            mapping = m.invoke(registry);
        } catch (Exception e) {
            return null;
        }
        if (mapping == null) {
            return null;
        }
        Method urlMap = findMethodAny(mapping.getClass(), "getUrlMap", 0);
        if (urlMap == null) {
            return null;
        }
        urlMap.setAccessible(true);
        Object map;
        try {
            map = urlMap.invoke(mapping);
        } catch (Exception e) {
            return null;
        }
        if (map instanceof Map<?, ?> mm) {
            for (Object v : mm.values()) {
                Object loc = firstLocation(v);
                if (loc != null) {
                    return loc;
                }
            }
        }
        return null;
    }

    static Object firstLocation(Object resourceHandler) {
        Method m = findMethodAny(resourceHandler.getClass(), "getLocations", 0);
        if (m == null) {
            return null;
        }
        m.setAccessible(true);
        Object v;
        try {
            v = m.invoke(resourceHandler);
        } catch (Exception e) {
            return null;
        }
        if (v instanceof Collection<?> c && !c.isEmpty()) {
            Object first = c.iterator().next();
            Method g = findMethodAny(first.getClass(), "getLocation", 0);
            if (g != null) {
                g.setAccessible(true);
                try {
                    return g.invoke(first);
                } catch (Exception e) {
                    return null;
                }
            }
            return String.valueOf(first);
        }
        return null;
    }

    /** 写字面量进观测（用例自己声明的标记值）。 */
    static void doConst(JsonNode step, ObjectNode res) {
        String as = step.path("as").asText("");
        if (!as.isEmpty()) {
            res.set(as, toJson(step.path("value")));
        }
    }

    /** 实例化一个类（无参构造），放进捕获池，可以当参数传回去。 */
    static void doInstantiate(JsonNode step, ObjectNode res) throws Exception {
        Class<?> k = Class.forName(step.path("class").asText());
        Object o = k.getDeclaredConstructor().newInstance();
        String as = step.path("as").asText("");
        if (!as.isEmpty()) {
            CAPTURED.put(as, o);
        }
    }

    /** 在某个对象上再调一个方法（任意可见性），结果进观测。 */
    static void doCallOn(JsonNode step, ObjectNode res) throws Exception {
        Object target = subject(step.path("subject").asText("target"));
        if (target == null) {
            return;
        }
        JsonNode args = step.path("args");
        Method m = findMethodAny(target.getClass(), step.path("method").asText(), args.size());
        if (m == null) {
            return;
        }
        m.setAccessible(true);
        Class<?>[] types = m.getParameterTypes();
        Object[] argv = new Object[types.length];
        for (int i = 0; i < types.length; i++) {
            argv[i] = i < args.size() ? decode(args.get(i), types[i]) : defaultValue(types[i]);
        }
        Object v = m.invoke(target, argv);
        String as = step.path("as").asText("");
        if (!as.isEmpty()) {
            res.set(as, toJson(v));
        }
    }

    /** ResponseBodyAdvice 的 supports 判定：给一组返回类型，逐个问「要不要包信封」。 */
    static void doAdviceSupports(JsonNode step, ObjectNode res) throws Exception {
        Class<?> adviceClass = Class.forName(step.path("class").asText());
        Object advice = adviceClass.getDeclaredConstructor().newInstance();
        Class<?> converter = Class.forName(step.path("converter").asText());
        Method supports = findMethodAny(adviceClass, "supports", 2);
        ObjectNode out = M.createObjectNode();
        var it = step.path("types").fields();
        while (it.hasNext()) {
            var e = it.next();
            Object param = methodParameterOf(Class.forName(e.getValue().asText()));
            out.put(e.getKey(), Boolean.TRUE.equals(supports.invoke(advice, param, converter)));
        }
        String as = step.path("as").asText("");
        if (!as.isEmpty()) {
            res.set(as, out);
        }
    }

    /** 路径事实：规范化后的文本 / URI 形式。 */
    static void doPathFact(JsonNode step, ObjectNode res) {
        Path p = Paths.get(step.path("value").asText("")).toAbsolutePath().normalize();
        String out = "uri".equals(step.path("mode").asText("text")) ? p.toUri().toString() : p.toString();
        String as = step.path("as").asText("");
        if (!as.isEmpty()) {
            res.put(as, out);
        }
    }

    static Object subject(String spec) {
        if (spec == null || spec.isEmpty() || "target".equals(spec)) {
            return TARGET;
        }
        if ("return".equals(spec)) {
            return LAST_RETURN;
        }
        if (spec.startsWith("fixture:")) {
            try {
                return fixture(spec.substring("fixture:".length()));
            } catch (Exception e) {
                return null;
            }
        }
        if (spec.startsWith("arg:")) {
            return LAST_ARGS.get(Integer.parseInt(spec.substring("arg:".length())));
        }
        if (spec.startsWith("captured:")) {
            return CAPTURED.get(spec.substring("captured:".length()));
        }
        return TARGET;
    }

}
