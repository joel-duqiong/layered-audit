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
 *   "types":    {"pkgItemReq": "com.x.PackageItemRequest"},
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
public class GenericFileHarness {

    static final ObjectMapper M = new ObjectMapper().findAndRegisterModules();

    static JsonNode ROOT;
    static JsonNode STUBS;
    static JsonNode FIXTURES;
    static JsonNode CTOR;
    static JsonNode TYPES;
    static JsonNode VIEWS;
    static JsonNode CASE_STUBS;
    static JsonNode CASE_FIXTURES;
    static JsonNode CASE_CTOR;

    static final Map<String, Object> FIXTURE_OBJECTS = new LinkedHashMap<>();
    static final Map<String, String> STUB_LABELS = new LinkedHashMap<>();
    static final Map<String, AtomicInteger> CALL_COUNTS = new ConcurrentHashMap<>();
    static final List<String> CALL_LOG = new ArrayList<>();
    static final Map<String, Object> CAPTURED = new LinkedHashMap<>();

    static Object TARGET;
    static Object LAST_RETURN;
    static Object LAST_RESPONSE;
    static final Map<Integer, Object> LAST_ARGS = new LinkedHashMap<>();
    static Path WORK_DIR;
    static Path CLASSES_ROOT;
    static Object logAppender;

    // ---------------------------------------------------------------- 入口

    public static void main(String[] args) throws Exception {
        WORK_DIR = Paths.get(args.length > 2 ? args[2] : System.getProperty("java.io.tmpdir"));
        CLASSES_ROOT = args.length > 3 && !args[3].isEmpty() ? Paths.get(args[3]) : null;
        ROOT = M.readTree(Paths.get(args[0]).toFile());
        STUBS = ROOT.path("stubs");
        FIXTURES = ROOT.path("fixtures");
        CTOR = ROOT.path("ctor");
        TYPES = ROOT.path("types");
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

    // ---------------------------------------------------------------- 场景

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

    // ---------------------------------------------------------------- 步骤

    static void runStep(JsonNode step, ObjectNode res) throws Exception {
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
            default -> res.put("step_unsupported", op);
        }
    }

    /** 方法调用：实例方法或静态方法。抛异常不中断场景（后续步骤继续跑，看中间态）。 */
    static void doCall(JsonNode step, ObjectNode res) throws Exception {
        String cls = step.path("class").asText("");
        Method method;
        Object receiver = null;
        if (!cls.isEmpty()) {
            method = pickMethod(allMethods(Class.forName(cls), step.path("method").asText()),
                    step.path("args").size());
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
        Object ret;
        try {
            ret = method.invoke(receiver, argv);
        } catch (InvocationTargetException e) {
            Throwable cause = e.getCause() == null ? e : e.getCause();
            res.put("result", "threw");
            recordError(res, cause);
            LAST_RETURN = null;
            return;
        }
        LAST_RETURN = ret;
        res.put("result", "ok");
        JsonNode capture = step.path("capture");
        var it = capture.fields();
        while (it.hasNext()) {
            var e = it.next();
            res.set(e.getKey(), captureValue(e.getValue(), ret));
        }
    }

    /** 把返回值上的路径抽成原始值。 */
    static JsonNode captureValue(JsonNode spec, Object value) {
        String path = spec.path("path").asText("");
        Object v = path.isEmpty() ? value : readPath(value, path);
        String mode = spec.path("op").asText("value");
        switch (mode) {
            case "count" -> {
                return M.getNodeFactory().numberNode(sizeOf(v));
            }
            case "json" -> {
                try {
                    return M.getNodeFactory().textNode(M.writeValueAsString(v));
                } catch (Exception e) {
                    return M.getNodeFactory().textNode(String.valueOf(v));
                }
            }
            case "text" -> {
                return M.getNodeFactory().textNode(String.valueOf(v));
            }
            default -> {
                return toJson(v);
            }
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
        res.set(step.path("as").asText(""), toJson(v));
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
        res.set(step.path("as").asText(name), toJson(v));
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
        Object configs = CAPTURED.get(step.path("from").asText(""));
        Map<?, ?> map = configs instanceof Map<?, ?> m ? m : Map.of();
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

    static Object corsProbes(Object cfg, JsonNode origins) throws Exception {
        List<Object> out = new ArrayList<>();
        if (cfg == null) {
            return out;
        }
        Method checkOrigin = cfg.getClass().getMethod("checkOrigin", String.class);
        checkOrigin.setAccessible(true);
        for (JsonNode o : origins) {
            String origin = o.asText();
            Object allowed = checkOrigin.invoke(cfg, origin);
            Map<String, Object> row = new LinkedHashMap<>();
            row.put("origin", origin);
            row.put("allowed", allowed != null);
            out.add(row);
        }
        return out;
    }

    /**
     * 路径语义事实：目录存在与否时 toUri() 的尾斜杠、Spring 相对路径拼接结果。
     * 纯文件系统语义，与项目无关；用完即删（零残留）。
     */
    static void doPathFacts(JsonNode step, ObjectNode res) throws Exception {
        Path base = Paths.get(step.path("base").asText(""));
        Files.createDirectories(base);
        for (JsonNode probe : step.path("probes")) {
            Path dir = base.resolve(probe.path("dir").asText("probe"));
            if (probe.path("create").asBoolean(false)) {
                Files.createDirectories(dir);
            } else {
                Files.deleteIfExists(dir);
            }
            String uri = dir.toAbsolutePath().normalize().toUri().toString();
            Object value = uri;
            String relative = probe.path("relative").asText("");
            if (!relative.isEmpty()) {
                value = resolveRelative(uri, relative);
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

    static String resolveRelative(String baseUri, String relative) {
        try {
            Class<?> urlResource = Class.forName("org.springframework.core.io.UrlResource");
            Object r = urlResource.getConstructor(String.class).newInstance(baseUri);
            Method createRelative = urlResource.getMethod("createRelative", String.class);
            Object child = createRelative.invoke(r, relative);
            Method getUrl = child.getClass().getMethod("getURL");
            return String.valueOf(getUrl.invoke(child));
        } catch (Exception e) {
            return "";
        }
    }

    // ---------------------------------------------------------------- 视图

    static JsonNode applyView(String viewName, Object subject) {
        JsonNode v = VIEWS.path(viewName);
        if (v.isMissingNode()) {
            return M.getNodeFactory().nullNode();
        }
        Object val = readPath(subject, v.path("source").asText(""));
        if (v.path("list").asBoolean(false) && val instanceof Collection<?> c) {
            ArrayNode arr = M.createArrayNode();
            for (Object o : c) {
                arr.add(projectFields(o, v.path("fields")));
            }
            return arr;
        }
        if (v.has("fields")) {
            return projectFields(val, v.path("fields"));
        }
        return toJson(val);
    }

    static JsonNode projectFields(Object o, JsonNode fields) {
        ObjectNode n = M.createObjectNode();
        var it = fields.fields();
        while (it.hasNext()) {
            var e = it.next();
            n.set(e.getKey(), toJson(readPath(o, e.getValue().asText(""))));
        }
        return n;
    }

    // ---------------------------------------------------------------- 度量

    static JsonNode measure(JsonNode spec, ObjectNode res) {
        String op = spec.path("op").asText("value");
        String of = spec.path("of").asText("");
        Object value = resolveMeasureSource(of, res);
        String path = spec.path("path").asText("");
        if (!path.isEmpty()) {
            value = readPath(value, path);
        }
        switch (op) {
            case "count" -> {
                return M.getNodeFactory().numberNode(sizeOf(value));
            }
            case "ends_with" -> {
                String s = String.valueOf(value);
                return M.getNodeFactory().booleanNode(s.endsWith(spec.path("suffix").asText("")));
            }
            case "contains" -> {
                String s = String.valueOf(value);
                return M.getNodeFactory().booleanNode(s.contains(spec.path("text").asText("")));
            }
            case "contains_all" -> {
                String s = String.valueOf(value);
                boolean all = true;
                for (JsonNode t : spec.path("texts")) {
                    if (!s.contains(t.asText())) {
                        all = false;
                    }
                }
                return M.getNodeFactory().booleanNode(all);
            }
            case "any_eq" -> {
                String field = spec.path("field").asText("");
                JsonNode want = spec.path("value");
                boolean hit = false;
                if (value instanceof Collection<?> c) {
                    for (Object o : c) {
                        Object f = readPath(o, field);
                        if (f != null && want != null && String.valueOf(f).equals(want.asText())) {
                            hit = true;
                            break;
                        }
                    }
                }
                return M.getNodeFactory().booleanNode(hit);
            }
            case "lookup" -> {
                String key = spec.path("key").asText("");
                String field = spec.path("field").asText("");
                JsonNode want = spec.path("value");
                Object found = null;
                if (value instanceof Collection<?> c) {
                    for (Object o : c) {
                        Object k = readPath(o, key);
                        if (k != null && want != null && String.valueOf(k).equals(want.asText())) {
                            found = readPath(o, field);
                            break;
                        }
                    }
                }
                return toJson(found);
            }
            case "all_of" -> {
                boolean all = true;
                for (JsonNode name : spec.path("of")) {
                    JsonNode v = res.get(name.asText());
                    if (v == null || !v.asBoolean(false)) {
                        all = false;
                    }
                }
                return M.getNodeFactory().booleanNode(all);
            }
            case "not" -> {
                return M.getNodeFactory().booleanNode(!truthy(value));
            }
            case "is_null" -> {
                return M.getNodeFactory().booleanNode(value == null);
            }
            case "log_contains" -> {
                String text = spec.path("text").asText("");
                boolean hit = false;
                for (String line : capturedLogs()) {
                    if (line.contains(text)) {
                        hit = true;
                        break;
                    }
                }
                return M.getNodeFactory().booleanNode(hit);
            }
            default -> {
                return toJson(value);
            }
        }
    }

    static Object resolveMeasureSource(String of, ObjectNode res) {
        if ("return".equals(of)) {
            return LAST_RETURN;
        }
        if ("logs".equals(of)) {
            return capturedLogs();
        }
        JsonNode v = res.get(of);
        if (v == null || v.isNull()) {
            return null;
        }
        if (v.isArray()) {
            List<Object> out = new ArrayList<>();
            v.forEach(out::add);
            return out;
        }
        if (v.isObject()) {
            Map<String, Object> out = new LinkedHashMap<>();
            v.fields().forEachRemaining(e -> out.put(e.getKey(), e.getValue().asText()));
            return out;
        }
        if (v.isNumber()) {
            return v.numberValue();
        }
        if (v.isBoolean()) {
            return v.booleanValue();
        }
        return v.asText();
    }

    static boolean truthy(Object v) {
        if (v == null) {
            return false;
        }
        if (v instanceof Boolean b) {
            return b;
        }
        if (v instanceof Collection<?> c) {
            return !c.isEmpty();
        }
        if (v instanceof Map<?, ?> m) {
            return !m.isEmpty();
        }
        return true;
    }

    static int sizeOf(Object v) {
        if (v == null) {
            return 0;
        }
        if (v instanceof Collection<?> c) {
            return c.size();
        }
        if (v instanceof Map<?, ?> m) {
            return m.size();
        }
        if (v instanceof String s) {
            return s.length();
        }
        if (v.getClass().isArray()) {
            return Array.getLength(v);
        }
        return 0;
    }

    // ---------------------------------------------------------------- 反射构造

    /** 反射选构造器，递归构造依赖图。 */
    static Object newTarget(String cls) throws Exception {
        Class<?> k = Class.forName(cls);
        Constructor<?> best = null;
        for (Constructor<?> c : k.getDeclaredConstructors()) {
            if (best == null || c.getParameterCount() < best.getParameterCount()) {
                best = c;
            }
        }
        if (best == null) {
            throw new IllegalArgumentException("目标类没有构造器: " + cls);
        }
        Class<?>[] types = best.getParameterTypes();
        Object[] deps = new Object[types.length];
        for (int i = 0; i < deps.length; i++) {
            JsonNode spec = ctorSpec(cls, i);
            deps[i] = spec.isMissingNode() ? dependency(types[i], 0, new ArrayList<>())
                    : buildValue(spec, types[i], new Object[0]);
        }
        best.setAccessible(true);
        return best.newInstance(deps);
    }

    static JsonNode ctorSpec(String cls, int index) {
        JsonNode from = CASE_CTOR.path(cls).path(String.valueOf(index));
        if (!from.isMissingNode()) {
            return from;
        }
        return CTOR.path(cls).path(String.valueOf(index));
    }

    /**
     * 依赖注入（通用）：像容器一样递归构造依赖图。
     *   接口 -> 动态代理（查打桩表）
     *   具体类 -> 递归构造它的构造器参数（深度上限防环）
     */
    static Object dependency(Class<?> t, int depth, List<Class<?>> path) {
        if (t.isInterface()) {
            return proxy(t);
        }
        if (t == String.class) {
            return "";
        }
        if (t.isPrimitive()) {
            return defaultValue(t);
        }
        if (depth > 6 || path.contains(t)) {
            return null;
        }
        try {
            Constructor<?> best = null;
            for (Constructor<?> c : t.getDeclaredConstructors()) {
                if (best == null || c.getParameterCount() < best.getParameterCount()) {
                    best = c;
                }
            }
            if (best == null) {
                return null;
            }
            Class<?>[] pt = best.getParameterTypes();
            Object[] vals = new Object[pt.length];
            path.add(t);
            for (int i = 0; i < pt.length; i++) {
                vals[i] = dependency(pt[i], depth + 1, path);
            }
            path.remove(t);
            best.setAccessible(true);
            return best.newInstance(vals);
        } catch (Exception e) {
            return null;
        }
    }

    static Object proxy(Class<?> iface) {
        InvocationHandler h = (p, m, a) -> {
            if (m.getDeclaringClass() == Object.class) {
                return objectMethod(p, m, a);
            }
            String label = STUB_LABELS.getOrDefault(iface.getName(), iface.getSimpleName());
            String key = label + "#" + m.getName();
            CALL_COUNTS.computeIfAbsent(key, k -> new AtomicInteger()).incrementAndGet();
            CALL_LOG.add(label + "." + m.getName());
            JsonNode spec = stubFor(iface, m.getName());
            if (spec == null || spec.isNull()) {
                return defaultValue(m.getReturnType());
            }
            if (spec.has("$countCalls")) {
                JsonNode rest = spec.deepCopy();
                ((ObjectNode) rest).remove("$countCalls");
                if (rest.size() == 0) {
                    return defaultValue(m.getReturnType());
                }
                spec = rest;
            }
            if (spec.has("$constAfterCalls")) {
                int n = spec.path("$constAfterCalls").path("n").asInt(0);
                if (CALL_COUNTS.get(key).get() <= n) {
                    return defaultValue(m.getReturnType());
                }
                return buildValue(spec.path("$constAfterCalls").path("value"), m.getReturnType(), a);
            }
            return buildValue(spec, m.getReturnType(), a);
        };
        return Proxy.newProxyInstance(GenericFileHarness.class.getClassLoader(),
                new Class<?>[]{iface}, h);
    }

    static Object objectMethod(Object p, Method m, Object[] a) {
        switch (m.getName()) {
            case "toString":
                return p.getClass().getSimpleName() + "@stub";
            case "hashCode":
                return System.identityHashCode(p);
            case "equals":
                return p == (a == null ? null : a[0]);
            default:
                return null;
        }
    }

    /** 打桩查找：先用例级（覆盖全局），再全局。接口名两种写法都认。 */
    static JsonNode stubFor(Class<?> iface, String method) {
        for (JsonNode stubs : new JsonNode[]{CASE_STUBS, STUBS}) {
            if (stubs == null) {
                continue;
            }
            JsonNode byIface = stubs.path(iface.getName());
            if (byIface.isMissingNode()) {
                byIface = stubs.path(iface.getSimpleName());
            }
            if (byIface.isMissingNode()) {
                continue;
            }
            JsonNode spec = byIface.path(method);
            if (!spec.isMissingNode()) {
                return spec;
            }
        }
        return null;
    }

    // ---------------------------------------------------------------- 值构造

    static Object buildValue(JsonNode spec, Class<?> type, Object[] args) {
        try {
            return buildValue0(spec, type, args);
        } catch (RuntimeException e) {
            throw e;
        } catch (Exception e) {
            throw new IllegalStateException("构造值失败: " + spec, e);
        }
    }

    static Object buildValue0(JsonNode spec, Class<?> type, Object[] args) throws Exception {
        if (spec == null || spec.isMissingNode() || spec.isNull()) {
            return defaultValue(type);
        }
        if (spec.has("$null")) {
            return type == Optional.class ? Optional.empty() : null;
        }
        if (spec.has("$const")) {
            return wrapOptional(coerce(spec.path("$const"), type), type);
        }
        if (spec.has("$echoArg0")) {
            Object v = args != null && args.length > 0 ? args[0] : defaultValue(type);
            return wrapOptional(v, type);
        }
        if (spec.has("$entity")) {
            Object e = buildEntity(spec.path("$entity").asText(), spec.path("fields"));
            return wrapOptional(e, type);
        }
        if (spec.has("$fixture")) {
            Object e = fixture(spec.path("$fixture").asText());
            return wrapOptional(e, type);
        }
        if (spec.has("$byKey")) {
            String k = args != null && args.length > 0 ? String.valueOf(args[0]) : "";
            JsonNode inner = spec.path("$byKey").path(k);
            if (inner.isMissingNode()) {
                return defaultValue(type);
            }
            return buildValue(inner, type, args);
        }
        if (spec.has("$byKeyMap")) {
            String k = args != null && args.length > 0 ? String.valueOf(args[0]) : "";
            JsonNode inner = spec.path("$byKeyMap").path("map").path(k);
            if (inner.isMissingNode()) {
                JsonNode def = spec.path("$byKeyMap").path("default");
                if (def.isMissingNode()) {
                    return defaultValue(type);
                }
                return buildValue(def, type, args);
            }
            return buildValue(inner, type, args);
        }
        if (spec.has("$list")) {
            List<Object> out = new ArrayList<>();
            for (JsonNode item : spec.path("$list")) {
                out.add(buildValue(item, Object.class, args));
            }
            return wrapOptional(out, type);
        }
        if (spec.has("$optional")) {
            return Optional.ofNullable(buildValue(spec.path("$optional"), Object.class, args));
        }
        if (spec.has("$optionalEmpty")) {
            return Optional.empty();
        }
        if (spec.has("$staticCall")) {
            return wrapOptional(staticCall(spec.path("$staticCall")), type);
        }
        if (spec.has("$throwable")) {
            return wrapOptional(throwable(spec.path("$throwable")), type);
        }
        return wrapOptional(coerce(spec, type), type);
    }

    static Object wrapOptional(Object v, Class<?> type) {
        if (type == Optional.class) {
            return Optional.ofNullable(v);
        }
        return v;
    }

    /** 静态工厂方法调用：{"class":..., "method":..., "args":[...]} */
    static Object staticCall(JsonNode spec) throws Exception {
        Class<?> k = Class.forName(spec.path("class").asText());
        Method m = pickMethod(allMethods(k, spec.path("method").asText()), spec.path("args").size());
        m.setAccessible(true);
        Class<?>[] types = m.getParameterTypes();
        Object[] argv = new Object[types.length];
        JsonNode args = spec.path("args");
        for (int i = 0; i < types.length; i++) {
            argv[i] = i < args.size() ? decode(args.get(i), types[i]) : defaultValue(types[i]);
        }
        return m.invoke(null, argv);
    }

    /** 构造异常：{"class":..., "message":..., "cause":{...}} 或 {"class":..., "ctorArgs":[...]} */
    static Object throwable(JsonNode spec) throws Exception {
        Class<?> k = Class.forName(spec.path("class").asText());
        Object cause = spec.has("cause") ? throwable(spec.path("cause")) : null;
        JsonNode ctorArgs = spec.path("ctorArgs");
        if (ctorArgs.size() > 0) {
            Constructor<?> c = null;
            for (Constructor<?> cand : k.getDeclaredConstructors()) {
                if (cand.getParameterCount() == ctorArgs.size()) {
                    c = cand;
                    break;
                }
            }
            if (c == null) {
                throw new IllegalArgumentException("找不到匹配的构造器: " + k.getName());
            }
            Class<?>[] pt = c.getParameterTypes();
            Object[] vals = new Object[pt.length];
            for (int i = 0; i < pt.length; i++) {
                vals[i] = decode(ctorArgs.get(i), pt[i]);
            }
            c.setAccessible(true);
            return c.newInstance(vals);
        }
        String message = spec.path("message").asText("");
        try {
            Constructor<?> c = k.getDeclaredConstructor(String.class, Throwable.class);
            c.setAccessible(true);
            return c.newInstance(message, cause);
        } catch (NoSuchMethodException ignored) {
            // 落到单参构造器
        }
        Constructor<?> c = k.getDeclaredConstructor(String.class);
        c.setAccessible(true);
        return c.newInstance(message);
    }

    /** 反射构造实体：按字段名找 setter，逐个赋值（支持嵌套 $entity）。 */
    static Object buildEntity(String fqcn, JsonNode fields) throws Exception {
        Class<?> k = Class.forName(fqcn);
        Object o = k.getDeclaredConstructor().newInstance();
        if (fields == null || fields.isEmpty()) {
            return o;
        }
        var it = fields.fields();
        while (it.hasNext()) {
            var e = it.next();
            Method setter = findSetter(k, e.getKey());
            if (setter == null) {
                continue;
            }
            setter.setAccessible(true);
            setter.invoke(o, buildValue(e.getValue(), setter.getParameterTypes()[0], new Object[0]));
        }
        return o;
    }

    /** 固定样本：同一场景内同一个实例（服务改的就是这个对象）。 */
    static Object fixture(String name) throws Exception {
        Object cached = FIXTURE_OBJECTS.get(name);
        if (cached != null) {
            return cached;
        }
        JsonNode spec = CASE_FIXTURES.path(name);
        if (spec.isMissingNode()) {
            spec = FIXTURES.path(name);
        }
        if (spec.isMissingNode()) {
            throw new IllegalArgumentException("用例文件里没有样本: " + name);
        }
        Object value = buildValue(spec, Object.class, new Object[0]);
        FIXTURE_OBJECTS.put(name, value);
        return value;
    }

    // ---------------------------------------------------------------- 参数解码

    static Object decode(JsonNode a, Class<?> type) throws Exception {
        if (a == null || a.isMissingNode() || a.isNull()) {
            return type == Optional.class ? Optional.empty() : null;
        }
        if (a.isObject() && a.has("$t")) {
            String t = a.get("$t").asText();
            switch (t) {
                case "null" -> {
                    return null;
                }
                case "long" -> {
                    return a.path("v").asLong();
                }
                case "int" -> {
                    return a.path("v").asInt();
                }
                case "str" -> {
                    return a.path("v").asText();
                }
                case "bool" -> {
                    return a.path("v").asBoolean();
                }
                case "dt" -> {
                    return OffsetDateTime.parse(a.path("v").asText());
                }
                case "enum" -> {
                    return enumValue(a, type);
                }
                case "entity" -> {
                    return buildEntity(a.path("class").asText(), a.path("fields"));
                }
                case "record" -> {
                    return recordValue(a.path("class").asText(), a.path("fields"));
                }
                case "fixture" -> {
                    return fixture(a.path("v").asText());
                }
                case "staticCall" -> {
                    return staticCall(a.path("staticCall"));
                }
                case "throwable" -> {
                    return throwable(a.path("throwable"));
                }
                case "class" -> {
                    return Class.forName(a.path("v").asText());
                }
                case "mediaType" -> {
                    return Class.forName("org.springframework.http.MediaType")
                            .getMethod("valueOf", String.class)
                            .invoke(null, a.path("v").asText());
                }
                case "methodParameter" -> {
                    return methodParameter(a);
                }
                case "instance" -> {
                    return dependency(Class.forName(a.path("class").asText()), 0, new ArrayList<>());
                }
                case "bytes" -> {
                    return Base64.getDecoder().decode(a.path("v").asText());
                }
                case "map" -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    a.path("v").fields().forEachRemaining(e -> m.put(e.getKey(), e.getValue().asText()));
                    return m;
                }
                default -> throw new IllegalArgumentException("未知参数类型 " + t);
            }
        }
        if (type == String.class) {
            return a.asText();
        }
        if (type == long.class || type == Long.class) {
            return a.asLong();
        }
        if (type == int.class || type == Integer.class) {
            return a.asInt();
        }
        if (type == boolean.class || type == Boolean.class) {
            return a.asBoolean();
        }
        if (type == double.class || type == Double.class) {
            return a.asDouble();
        }
        if (Collection.class.isAssignableFrom(type)) {
            List<Object> out = new ArrayList<>();
            for (JsonNode el : a) {
                out.add(decode(el, Object.class));
            }
            return out;
        }
        if (Map.class.isAssignableFrom(type)) {
            Map<String, Object> out = new LinkedHashMap<>();
            a.fields().forEachRemaining(e -> out.put(e.getKey(), e.getValue().asText()));
            return out;
        }
        try {
            return M.treeToValue(a, type);
        } catch (Exception e) {
            throw new IllegalArgumentException("cannot decode " + a + " as " + type, e);
        }
    }

    /** record 用 Jackson 还原（Jackson 2.12+ 原生支持 record）。 */
    static Object recordValue(String fqcn, JsonNode fields) throws Exception {
        Class<?> k = Class.forName(fqcn);
        try {
            return M.treeToValue(fields == null ? M.createObjectNode() : fields, k);
        } catch (Exception e) {
            Constructor<?> best = null;
            for (Constructor<?> c : k.getDeclaredConstructors()) {
                if (best == null || c.getParameterCount() < best.getParameterCount()) {
                    best = c;
                }
            }
            best.setAccessible(true);
            Class<?>[] pt = best.getParameterTypes();
            Object[] vals = new Object[pt.length];
            for (int i = 0; i < pt.length; i++) {
                JsonNode v = fields == null ? null : fields.get(pt[i].getSimpleName());
                vals[i] = v == null ? defaultValue(pt[i]) : decode(v, pt[i]);
            }
            return best.newInstance(vals);
        }
    }

    /** MethodParameter：可以只给类型（用它的单参构造器），也可以给类+方法。 */
    static Object methodParameter(JsonNode a) throws Exception {
        Class<?> mp = Class.forName("org.springframework.core.MethodParameter");
        if (a.has("type")) {
            Class<?> t = Class.forName(a.path("type").asText());
            Constructor<?> c = mp.getDeclaredConstructor(Class.class, int.class);
            c.setAccessible(true);
            return c.newInstance(t, -1);
        }
        Class<?> k = Class.forName(a.path("class").asText());
        Method m = findDeclared(k, a.path("method").asText());
        if (m == null) {
            throw new IllegalArgumentException("找不到方法 " + a.path("method").asText());
        }
        Constructor<?> c = mp.getDeclaredConstructor(Method.class, int.class);
        c.setAccessible(true);
        return c.newInstance(m, -1);
    }

    static Object enumValue(JsonNode a, Class<?> type) {
        Class<?> k = type;
        String cls = a.path("class").asText("");
        if (!cls.isEmpty()) {
            try {
                k = Class.forName(cls);
            } catch (ClassNotFoundException ignored) {
                // 用入参类型
            }
        }
        String name = a.path("v").asText();
        if (k != null && k.isEnum()) {
            for (Object c : k.getEnumConstants()) {
                if (c.toString().equals(name)) {
                    return c;
                }
            }
        }
        throw new IllegalArgumentException("枚举值解析不了: " + name);
    }

    /** 框架类型参数自动补齐（Servlet 请求/响应、过滤器链、Spring 的请求响应包装）。 */
    static Object autoArg(Class<?> type, JsonNode step) {
        String name = type.getName();
        try {
            switch (name) {
                case "jakarta.servlet.http.HttpServletRequest":
                case "jakarta.servlet.ServletRequest": {
                    return mockRequest(step.path("request"));
                }
                case "jakarta.servlet.http.HttpServletResponse":
                case "jakarta.servlet.ServletResponse": {
                    Object resp = mockResponse();
                    LAST_RESPONSE = resp;
                    return resp;
                }
                case "jakarta.servlet.FilterChain": {
                    return Class.forName("org.springframework.mock.web.MockFilterChain")
                            .getDeclaredConstructor().newInstance();
                }
                case "org.springframework.http.server.ServerHttpRequest": {
                    Class<?> servletRequest = Class.forName("jakarta.servlet.http.HttpServletRequest");
                    Class<?> c = Class.forName("org.springframework.http.server.ServletServerHttpRequest");
                    Constructor<?> ctor = c.getDeclaredConstructor(servletRequest);
                    ctor.setAccessible(true);
                    return ctor.newInstance(mockRequest(step.path("request")));
                }
                case "org.springframework.http.server.ServerHttpResponse": {
                    Class<?> servletResponse = Class.forName("jakarta.servlet.http.HttpServletResponse");
                    Class<?> c = Class.forName("org.springframework.http.server.ServletServerHttpResponse");
                    Constructor<?> ctor = c.getDeclaredConstructor(servletResponse);
                    ctor.setAccessible(true);
                    Object resp = mockResponse();
                    LAST_RESPONSE = resp;
                    return ctor.newInstance(resp);
                }
                default:
                    return defaultValue(type);
            }
        } catch (Exception e) {
            return defaultValue(type);
        }
    }

    static Object mockRequest(JsonNode spec) throws Exception {
        Class<?> k = Class.forName("org.springframework.mock.web.MockHttpServletRequest");
        Object req = k.getDeclaredConstructor(String.class, String.class)
                .newInstance(spec.path("method").asText("GET"), spec.path("uri").asText("/"));
        String remote = spec.path("remote_addr").asText("");
        if (!remote.isEmpty()) {
            invokeIfPresent(req, "setRemoteAddr", new Class<?>[]{String.class}, remote);
        }
        var headers = spec.path("headers").fields();
        while (headers.hasNext()) {
            var e = headers.next();
            invokeIfPresent(req, "addHeader", new Class<?>[]{String.class, String.class},
                    e.getKey(), e.getValue().asText());
        }
        return req;
    }

    static Object mockResponse() throws Exception {
        Class<?> k = Class.forName("org.springframework.mock.web.MockHttpServletResponse");
        Object resp = k.getDeclaredConstructor().newInstance();
        LAST_RESPONSE = resp;
        return resp;
    }

    static void invokeIfPresent(Object target, String method, Class<?>[] types, Object... args) {
        try {
            Method m = target.getClass().getMethod(method, types);
            m.invoke(target, args);
        } catch (Exception ignored) {
            // 没有这个方法就算了
        }
    }

    // ---------------------------------------------------------------- 方法查找

    static List<Method> allMethods(Class<?> type, String name) {
        List<Method> out = new ArrayList<>();
        for (Class<?> c = type; c != null; c = c.getSuperclass()) {
            for (Method m : c.getDeclaredMethods()) {
                if (m.getName().equals(name)) {
                    out.add(m);
                }
            }
        }
        return out;
    }

    static Method findDeclared(Class<?> type, String name) {
        for (Class<?> c = type; c != null; c = c.getSuperclass()) {
            for (Method m : c.getDeclaredMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == 0) {
                    return m;
                }
            }
        }
        return null;
    }

    /** 选方法：参数个数匹配的优先；尾部参数允许由框架类型自动补齐。 */
    static Method pickMethod(List<Method> cands, int provided) {
        Method fallback = null;
        for (Method m : cands) {
            if (fallback == null) {
                fallback = m;
            }
            Class<?>[] pt = m.getParameterTypes();
            if (pt.length < provided) {
                continue;
            }
            boolean ok = true;
            for (int i = provided; i < pt.length; i++) {
                if (!isAutoFillable(pt[i])) {
                    ok = false;
                    break;
                }
            }
            if (ok) {
                return m;
            }
        }
        return fallback;
    }

    static boolean isAutoFillable(Class<?> t) {
        String n = t.getName();
        return n.equals("jakarta.servlet.http.HttpServletRequest")
                || n.equals("jakarta.servlet.ServletRequest")
                || n.equals("jakarta.servlet.http.HttpServletResponse")
                || n.equals("jakarta.servlet.ServletResponse")
                || n.equals("jakarta.servlet.FilterChain")
                || n.equals("org.springframework.http.server.ServerHttpRequest")
                || n.equals("org.springframework.http.server.ServerHttpResponse");
    }

    static Method findSetter(Class<?> k, String prop) {
        String name = "set" + Character.toUpperCase(prop.charAt(0)) + prop.substring(1);
        for (Class<?> c = k; c != null; c = c.getSuperclass()) {
            for (Method m : c.getMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == 1) {
                    return m;
                }
            }
            for (Method m : c.getDeclaredMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == 1) {
                    return m;
                }
            }
        }
        return null;
    }

    static Field findField(Class<?> k, String name) {
        for (Class<?> c = k; c != null; c = c.getSuperclass()) {
            try {
                return c.getDeclaredField(name);
            } catch (NoSuchFieldException ignored) {
                // 往父类找
            }
        }
        return null;
    }

    // ---------------------------------------------------------------- 路径读取

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

    /** 路径读取：a.b.c -> getA().getB().getC()；也支持直接的方法名（value 之类）。 */
    static Object readPath(Object o, String path) {
        if (o == null || path == null || path.isEmpty()) {
            return o;
        }
        Object cur = o;
        for (String seg : path.split("\\.")) {
            if (cur == null) {
                return null;
            }
            cur = readSegment(cur, seg);
        }
        return cur;
    }

    static Object readSegment(Object o, String seg) {
        String[] candidates = {seg, "get" + Character.toUpperCase(seg.charAt(0)) + seg.substring(1),
                "is" + Character.toUpperCase(seg.charAt(0)) + seg.substring(1)};
        for (String candidate : candidates) {
            try {
                Method m = o.getClass().getMethod(candidate);
                m.setAccessible(true);
                return m.invoke(o);
            } catch (NoSuchMethodException ignored) {
                // 试下一个
            } catch (Exception e) {
                return null;
            }
        }
        return null;
    }

    static Object callNoArg(Object target, String method) {
        if (target == null) {
            return null;
        }
        try {
            Method m = target.getClass().getMethod(method);
            m.setAccessible(true);
            return m.invoke(target);
        } catch (Exception e) {
            return null;
        }
    }

    // ---------------------------------------------------------------- 观察输出

    static JsonNode toJson(Object o) {
        if (o == null) {
            return M.getNodeFactory().nullNode();
        }
        try {
            return M.valueToTree(o);
        } catch (Exception e) {
            try {
                return reflectiveDump(o);
            } catch (Exception e2) {
                return M.getNodeFactory().textNode(String.valueOf(o));
            }
        }
    }

    static JsonNode reflectiveDump(Object o) throws Exception {
        ObjectNode n = M.createObjectNode();
        Class<?> k = o.getClass();
        if (k.isRecord()) {
            for (var c : k.getRecordComponents()) {
                try {
                    Method acc = c.getAccessor();
                    acc.setAccessible(true);
                    n.set(c.getName(), toJson(acc.invoke(o)));
                } catch (Exception ignored) {
                    // 读不到就跳过
                }
            }
            return n;
        }
        for (Method m : k.getMethods()) {
            if (m.getParameterCount() != 0 || m.getDeclaringClass() == Object.class) {
                continue;
            }
            String name = m.getName();
            if (name.startsWith("get") && name.length() > 3) {
                name = name.substring(3);
            } else if (name.startsWith("is") && name.length() > 2) {
                name = name.substring(2);
            } else {
                continue;
            }
            name = Character.toLowerCase(name.charAt(0)) + name.substring(1);
            try {
                m.setAccessible(true);
                n.set(name, toJson(m.invoke(o)));
            } catch (Exception ignored) {
                // 读不到就跳过
            }
        }
        return n;
    }

    /** 通用实体状态：反射读所有可读属性。 */
    static Map<String, Object> entityState(Object o) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (o == null) {
            return out;
        }
        for (Method m : o.getClass().getMethods()) {
            if (m.getParameterCount() != 0 || m.getDeclaringClass() == Object.class) {
                continue;
            }
            String name = m.getName();
            if (name.startsWith("get") && name.length() > 3) {
                name = name.substring(3);
            } else if (name.startsWith("is") && name.length() > 2) {
                name = name.substring(2);
            } else {
                continue;
            }
            name = Character.toLowerCase(name.charAt(0)) + name.substring(1);
            try {
                m.setAccessible(true);
                out.put(name, m.invoke(o));
            } catch (Exception ignored) {
                // 读不到就跳过
            }
        }
        return out;
    }

    static void recordError(ObjectNode res, Throwable t) {
        boolean business = false;
        try {
            Method getCode = t.getClass().getMethod("getErrorCode");
            Object code = getCode.invoke(t);
            Method getStatus = t.getClass().getMethod("getStatus");
            Object st = getStatus.invoke(t);
            if (st != null) {
                Object v = st.getClass().getMethod("value").invoke(st);
                res.put("http_status", ((Number) v).intValue());
            }
            res.put("error_type", t.getClass().getSimpleName());
            res.put("error_code", code instanceof Enum<?> e ? e.name() : String.valueOf(code));
            res.put("error_message", String.valueOf(t.getMessage()));
            business = true;
        } catch (Exception ignored) {
            // 不是业务异常
        }
        if (!business) {
            res.put("error_type", t.getClass().getName());
            res.put("error_message", String.valueOf(t.getMessage()));
            res.put("is_business_exception", false);
        }
    }

    static Object defaultValue(Class<?> t) {
        if (t == Optional.class) {
            return Optional.empty();
        }
        if (t == boolean.class || t == Boolean.class) {
            return Boolean.FALSE;
        }
        if (t == int.class || t == Integer.class) {
            return 0;
        }
        if (t == long.class || t == Long.class) {
            return 0L;
        }
        if (t == double.class || t == Double.class) {
            return 0d;
        }
        if (t == float.class || t == Float.class) {
            return 0f;
        }
        if (t == short.class || t == Short.class) {
            return (short) 0;
        }
        if (t == byte.class || t == Byte.class) {
            return (byte) 0;
        }
        if (t == char.class || t == Character.class) {
            return (char) 0;
        }
        if (Collection.class.isAssignableFrom(t)) {
            return new ArrayList<>();
        }
        if (Map.class.isAssignableFrom(t)) {
            return new LinkedHashMap<>();
        }
        if (t == String.class) {
            return "";
        }
        return null;
    }

    static Object coerce(JsonNode v, Class<?> type) {
        if (type == Optional.class) {
            return v.isNull() ? Optional.empty() : Optional.of(coerce(v, Object.class));
        }
        if (v.isNull()) {
            return defaultValue(type);
        }
        if (type == String.class) {
            return v.asText();
        }
        if (type == long.class || type == Long.class) {
            return v.asLong();
        }
        if (type == int.class || type == Integer.class) {
            return v.asInt();
        }
        if (type == boolean.class || type == Boolean.class) {
            return v.asBoolean();
        }
        if (type == double.class || type == Double.class) {
            return v.asDouble();
        }
        if (type == float.class || type == Float.class) {
            return (float) v.asDouble();
        }
        if (type == short.class || type == Short.class) {
            return (short) v.asInt();
        }
        if (type == byte.class || type == Byte.class) {
            return (byte) v.asInt();
        }
        if (type == char.class || type == Character.class) {
            String s = v.asText();
            return s.isEmpty() ? (char) 0 : s.charAt(0);
        }
        if (Collection.class.isAssignableFrom(type)) {
            List<Object> out = new ArrayList<>();
            if (v.isArray()) {
                for (JsonNode item : v) {
                    out.add(buildValue(item, Object.class, new Object[0]));
                }
            }
            return out;
        }
        if (type.isEnum() && v.isTextual()) {
            for (Object c : type.getEnumConstants()) {
                if (c.toString().equals(v.asText())) {
                    return c;
                }
            }
        }
        return v;
    }

    // ---------------------------------------------------------------- 日志捕获

    static void attachLogCapture(String pkg) {
        try {
            Class<?> ctxClass = Class.forName("ch.qos.logback.classic.LoggerContext");
            Object ctx = org.slf4j.LoggerFactory.getILoggerFactory();
            if (!ctxClass.isInstance(ctx)) {
                return;
            }
            Class<?> appenderClass = Class.forName("ch.qos.logback.core.read.ListAppender");
            Object appender = appenderClass.getDeclaredConstructor().newInstance();
            appenderClass.getMethod("setContext", ctxClass).invoke(appender, ctx);
            appenderClass.getMethod("start").invoke(appender);
            Method getLogger = ctxClass.getMethod("getLogger", String.class);
            Object logger = getLogger.invoke(ctx, pkg);
            logger.getClass().getMethod("addAppender",
                    Class.forName("ch.qos.logback.core.Appender")).invoke(logger, appender);
            logAppender = appender;
        } catch (Throwable t) {
            logAppender = null;
        }
    }

    static List<String> capturedLogs() {
        List<String> out = new ArrayList<>();
        if (logAppender == null) {
            return out;
        }
        try {
            List<?> events = (List<?>) logAppender.getClass().getMethod("getList").invoke(logAppender);
            for (Object e : events) {
                Object msg = e.getClass().getMethod("getFormattedMessage").invoke(e);
                out.add(String.valueOf(msg));
            }
        } catch (Exception ignored) {
            // 读不到就当没有日志
        }
        return out;
    }

    static void clearLogs() {
        if (logAppender == null) {
            return;
        }
        try {
            Object list = logAppender.getClass().getMethod("getList").invoke(logAppender);
            ((List<?>) list).clear();
        } catch (Exception ignored) {
            // 清不掉就清不掉
        }
    }

    // ---------------------------------------------------------------- 通用检查器

    static ObjectNode runDetectors() {
        ObjectNode out = M.createObjectNode();
        JsonNode det = ROOT.path("detectors");
        List<String> enabled = new ArrayList<>();
        for (JsonNode n : det.path("enable")) {
            enabled.add(n.asText());
        }
        if (enabled.isEmpty()) {
            return out;
        }
        List<String> classes = scanClasses();
        out.put("classes_scanned", classes.size());
        for (String name : enabled) {
            ArrayNode arr = M.createArrayNode();
            try {
                switch (name) {
                    case "override_postcondition" -> arr = detectOverridePostcondition(classes, det);
                    case "profile_assembly" -> arr = detectProfileAssembly(classes);
                    case "entity_value_equality" -> arr = detectEntityValueEquality(classes);
                    case "cacheable_mutable" -> arr = detectCacheableMutable(classes);
                    case "encapsulation_leak" -> arr = detectEncapsulationLeak(classes);
                    case "outbound_http" -> arr = detectOutboundHttp(classes);
                    default -> {
                        // 未知的检查器忽略
                    }
                }
            } catch (Throwable t) {
                ObjectNode err = M.createObjectNode();
                err.put("detector_error", t.toString());
                arr.add(err);
            }
            out.set(name, arr);
        }
        return out;
    }

    static List<String> scanClasses() {
        List<String> out = new ArrayList<>();
        if (CLASSES_ROOT == null || !Files.isDirectory(CLASSES_ROOT)) {
            return out;
        }
        try (var stream = Files.walk(CLASSES_ROOT)) {
            List<Path> files = stream.filter(p -> p.toString().endsWith(".class")).toList();
            for (Path p : files) {
                String rel = CLASSES_ROOT.relativize(p).toString();
                if (rel.contains("$")) {
                    continue;
                }
                String fqcn = rel.substring(0, rel.length() - ".class".length())
                        .replace(java.io.File.separatorChar, '.').replace('/', '.');
                out.add(fqcn);
            }
        } catch (IOException ignored) {
            // 扫不到就当没有类
        }
        out.sort(String::compareTo);
        return out;
    }

    /**
     * 覆写后置条件：子类覆写与父类实现对同一入参产生的可观察状态不一致
     * —— 里氏替换违反的机械判据。
     */
    static ArrayNode detectOverridePostcondition(List<String> classes, JsonNode det) {
        ArrayNode arr = M.createArrayNode();
        for (String cn : classes) {
            Class<?> k;
            try {
                k = Class.forName(cn);
            } catch (Throwable t) {
                continue;
            }
            for (Method m : k.getDeclaredMethods()) {
                if (m.isSynthetic() || Modifier.isStatic(m.getModifiers()) || m.isBridge()) {
                    continue;
                }
                Method sup = findSuperMethod(k, m);
                if (sup == null) {
                    continue;
                }
                JsonNode argsSpec = det.path("args").path(cn + "#" + m.getName());
                if (argsSpec.isMissingNode()) {
                    argsSpec = det.path("args").path(m.getName());
                }
                if (argsSpec.isMissingNode()) {
                    continue;   // 没有样例入参就不下结论
                }
                try {
                    Object instA = newTarget(cn);
                    Object instB = newTarget(cn);
                    if (instA == null || instB == null) {
                        continue;
                    }
                    Class<?>[] pt = m.getParameterTypes();
                    Object[] argv = new Object[pt.length];
                    for (int i = 0; i < pt.length; i++) {
                        JsonNode a = i < argsSpec.size() ? argsSpec.get(i) : null;
                        argv[i] = a == null ? defaultValue(pt[i]) : decode(a, pt[i]);
                    }
                    sup.setAccessible(true);
                    m.setAccessible(true);
                    sup.invoke(instA, argv);
                    m.invoke(instB, argv);
                    Map<String, Object> stateA = entityState(instA);
                    Map<String, Object> stateB = entityState(instB);
                    List<String> diff = new ArrayList<>();
                    for (String key : stateA.keySet()) {
                        Object va = stateA.get(key);
                        Object vb = stateB.get(key);
                        if (va == null ? vb != null : !va.equals(vb)) {
                            diff.add(key);
                        }
                    }
                    if (!diff.isEmpty()) {
                        ObjectNode row = M.createObjectNode();
                        row.put("class", cn);
                        row.put("method", m.getName());
                        row.put("super_class", sup.getDeclaringClass().getName());
                        row.set("divergent_fields", M.valueToTree(diff));
                        row.set("super_state", M.valueToTree(stateA));
                        row.set("override_state", M.valueToTree(stateB));
                        arr.add(row);
                    }
                } catch (Throwable t) {
                    // 构造/调用失败就不下结论
                }
            }
        }
        return arr;
    }

    static Method findSuperMethod(Class<?> k, Method m) {
        for (Class<?> c = k.getSuperclass(); c != null; c = c.getSuperclass()) {
            for (Method cand : c.getDeclaredMethods()) {
                if (cand.getName().equals(m.getName())
                        && cand.getParameterCount() == m.getParameterCount()) {
                    return cand;
                }
            }
        }
        return null;
    }

    /** 条件装配：@Profile 决定了同一接口的不同实现走哪一条。 */
    static ArrayNode detectProfileAssembly(List<String> classes) {
        ArrayNode arr = M.createArrayNode();
        for (String cn : classes) {
            Class<?> k;
            try {
                k = Class.forName(cn);
            } catch (Throwable t) {
                continue;
            }
            List<String> profiles = profileValues(k);
            if (profiles.isEmpty()) {
                continue;
            }
            ObjectNode row = M.createObjectNode();
            row.put("class", cn);
            row.set("profiles", M.valueToTree(profiles));
            ArrayNode siblings = M.createArrayNode();
            for (String other : classes) {
                if (other.equals(cn)) {
                    continue;
                }
                Class<?> o;
                try {
                    o = Class.forName(other);
                } catch (Throwable t) {
                    continue;
                }
                List<String> op = profileValues(o);
                if (op.isEmpty()) {
                    continue;
                }
                for (Class<?> iface : k.getInterfaces()) {
                    if (iface.isAssignableFrom(o)) {
                        ObjectNode s = M.createObjectNode();
                        s.put("class", other);
                        s.set("profiles", M.valueToTree(op));
                        s.put("interface", iface.getName());
                        siblings.add(s);
                        break;
                    }
                }
            }
            row.set("siblings", siblings);
            arr.add(row);
        }
        return arr;
    }

    static List<String> profileValues(Class<?> k) {
        List<String> out = new ArrayList<>();
        try {
            Class<?> profile = Class.forName("org.springframework.context.annotation.Profile");
            for (var a : k.getAnnotations()) {
                if (profile.isInstance(a)) {
                    Object v = profile.getMethod("value").invoke(a);
                    if (v instanceof String[] arr) {
                        out.addAll(List.of(arr));
                    } else if (v != null) {
                        out.add(String.valueOf(v));
                    }
                }
            }
        } catch (Throwable ignored) {
            // 没有 Profile 注解
        }
        return out;
    }

    /** 实体无值相等：@Entity 类没有覆写 equals/hashCode —— 语义是「同一个实例」。 */
    static ArrayNode detectEntityValueEquality(List<String> classes) {
        ArrayNode arr = M.createArrayNode();
        for (String cn : classes) {
            Class<?> k;
            try {
                k = Class.forName(cn);
            } catch (Throwable t) {
                continue;
            }
            if (!hasAnnotation(k, "jakarta.persistence.Entity")) {
                continue;
            }
            boolean hasEquals = declaresMethod(k, "equals", 1);
            boolean hasHashCode = declaresMethod(k, "hashCode", 0);
            if (hasEquals && hasHashCode) {
                continue;
            }
            ObjectNode row = M.createObjectNode();
            row.put("class", cn);
            row.put("has_equals", hasEquals);
            row.put("has_hashcode", hasHashCode);
            try {
                Object a = k.getDeclaredConstructor().newInstance();
                Object b = k.getDeclaredConstructor().newInstance();
                row.put("two_instances_equal", a.equals(b));
            } catch (Throwable t) {
                row.put("two_instances_equal", false);
            }
            arr.add(row);
        }
        return arr;
    }

    /** 缓存值可变性：@Cacheable 方法把什么放进了缓存。 */
    static ArrayNode detectCacheableMutable(List<String> classes) {
        ArrayNode arr = M.createArrayNode();
        Class<?> cacheable;
        try {
            cacheable = Class.forName("org.springframework.cache.annotation.Cacheable");
        } catch (Throwable t) {
            return arr;
        }
        for (String cn : classes) {
            Class<?> k;
            try {
                k = Class.forName(cn);
            } catch (Throwable t) {
                continue;
            }
            for (Method m : k.getDeclaredMethods()) {
                boolean isCacheable = false;
                String cacheName = "";
                for (var a : m.getAnnotations()) {
                    if (!cacheable.isInstance(a)) {
                        continue;
                    }
                    isCacheable = true;
                    try {
                        Object v = cacheable.getMethod("value").invoke(a);
                        if (v instanceof String[] arr2 && arr2.length > 0) {
                            cacheName = arr2[0];
                        } else if (v != null) {
                            cacheName = String.valueOf(v);
                        }
                    } catch (Exception ignored) {
                        // 取不到缓存名就不写
                    }
                }
                if (!isCacheable) {
                    continue;
                }
                ObjectNode row = M.createObjectNode();
                row.put("class", cn);
                row.put("method", m.getName());
                row.put("cache", cacheName);
                row.put("return_type", m.getGenericReturnType().getTypeName());
                Class<?> element = elementType(m);
                row.put("element_type", element == null ? "" : element.getName());
                row.put("container_mutable", element != null);
                row.put("element_mutable", element != null && isMutableType(element));
                try {
                    Object inst = newTarget(cn);
                    if (inst != null && m.getParameterCount() == 0) {
                        m.setAccessible(true);
                        Object v = m.invoke(inst);
                        row.set("returned", toJson(v));
                        row.put("mutation_rejected", !tryMutate(v));
                        row.put("same_instance_twice", sameInstance(inst, m));
                    }
                } catch (Throwable t) {
                    row.put("probe_error", t.toString());
                }
                arr.add(row);
            }
        }
        return arr;
    }

    static boolean sameInstance(Object inst, Method m) {
        try {
            Object a = m.invoke(inst);
            Object b = m.invoke(inst);
            return a == b;
        } catch (Throwable t) {
            return false;
        }
    }

    @SuppressWarnings("unchecked")
    static boolean tryMutate(Object v) {
        try {
            if (v instanceof Collection<?> c) {
                ((Collection<Object>) c).add(null);
                return true;
            }
            if (v instanceof Map<?, ?> m) {
                ((Map<Object, Object>) m).put("__probe__", null);
                return true;
            }
            if (v != null && v.getClass().isArray()) {
                Array.set(v, 0, null);
                return true;
            }
        } catch (Throwable t) {
            return false;
        }
        return false;
    }

    /** public 方法返回可变实体 -> 封装泄漏。 */
    static ArrayNode detectEncapsulationLeak(List<String> classes) {
        ArrayNode arr = M.createArrayNode();
        for (String cn : classes) {
            Class<?> k;
            try {
                k = Class.forName(cn);
            } catch (Throwable t) {
                continue;
            }
            for (Method m : k.getDeclaredMethods()) {
                if (!Modifier.isPublic(m.getModifiers()) || Modifier.isStatic(m.getModifiers())) {
                    continue;
                }
                Class<?> element = elementType(m);
                if (element == null || !isMutableType(element)) {
                    continue;
                }
                ObjectNode row = M.createObjectNode();
                row.put("class", cn);
                row.put("method", m.getName());
                row.put("return_type", m.getGenericReturnType().getTypeName());
                row.put("element_type", element.getName());
                row.put("element_entity", hasAnnotation(element, "jakarta.persistence.Entity"));
                arr.add(row);
            }
        }
        return arr;
    }

    static Class<?> elementType(Method m) {
        Class<?> rt = m.getReturnType();
        if (rt == List.class || rt == Collection.class || rt == Set.class || rt == Iterable.class) {
            java.lang.reflect.Type gt = m.getGenericReturnType();
            if (gt instanceof java.lang.reflect.ParameterizedType pt) {
                java.lang.reflect.Type[] args = pt.getActualTypeArguments();
                if (args.length == 1 && args[0] instanceof Class<?> c) {
                    return c;
                }
            }
            return Object.class;
        }
        if (rt.isArray()) {
            return rt.getComponentType();
        }
        return null;
    }

    /** 可变类型：有 setter 的 POJO / 实体（record 与不可变值对象不算）。 */
    static boolean isMutableType(Class<?> k) {
        if (k == null) {
            return false;
        }
        if (k.isRecord() || k.isEnum() || k.isPrimitive()) {
            return false;
        }
        if (k.getName().startsWith("java.")) {
            return false;
        }
        if (hasAnnotation(k, "jakarta.persistence.Entity")) {
            return true;
        }
        for (Method m : k.getMethods()) {
            if (m.getName().startsWith("set") && m.getParameterCount() == 1) {
                return true;
            }
        }
        return false;
    }

    /** 跨仓库 HTTP 调用：类里内嵌远端 URL，或持有 HTTP 客户端。 */
    static ArrayNode detectOutboundHttp(List<String> classes) {
        ArrayNode arr = M.createArrayNode();
        for (String cn : classes) {
            ObjectNode row = M.createObjectNode();
            row.put("class", cn);
            ArrayNode urls = M.createArrayNode();
            if (CLASSES_ROOT != null) {
                Path file = CLASSES_ROOT.resolve(cn.replace('.', '/') + ".class");
                try {
                    String text = new String(Files.readAllBytes(file), StandardCharsets.ISO_8859_1);
                    Matcher m = Pattern.compile("https?://[^\"\\s]{4,}").matcher(text);
                    Set<String> found = new LinkedHashSet<>();
                    while (m.find()) {
                        found.add(m.group());
                    }
                    found.forEach(urls::add);
                } catch (IOException ignored) {
                    // 读不到 class 文件就跳过
                }
            }
            row.set("embedded_urls", urls);
            ArrayNode clients = M.createArrayNode();
            try {
                Class<?> k = Class.forName(cn);
                for (Field f : k.getDeclaredFields()) {
                    if (isHttpClientType(f.getType())) {
                        ObjectNode c = M.createObjectNode();
                        c.put("field", f.getName());
                        c.put("type", f.getType().getName());
                        clients.add(c);
                    }
                }
            } catch (Throwable ignored) {
                // 类加载不了就跳过
            }
            row.set("http_client_fields", clients);
            if (urls.size() > 0 || clients.size() > 0) {
                arr.add(row);
            }
        }
        return arr;
    }

    static boolean isHttpClientType(Class<?> t) {
        String n = t.getName();
        return n.equals("java.net.http.HttpClient")
                || n.equals("java.net.URL")
                || n.equals("java.net.HttpURLConnection")
                || n.equals("org.springframework.web.client.RestTemplate")
                || n.equals("org.springframework.web.reactive.function.client.WebClient")
                || n.startsWith("org.apache.http.client")
                || n.equals("okhttp3.OkHttpClient");
    }

    static boolean hasAnnotation(Class<?> k, String annotation) {
        try {
            Class<?> a = Class.forName(annotation);
            for (var ann : k.getAnnotations()) {
                if (a.isInstance(ann)) {
                    return true;
                }
            }
        } catch (Throwable ignored) {
            // 注解类不在 classpath 上
        }
        return false;
    }

    static boolean declaresMethod(Class<?> k, String name, int arity) {
        for (Class<?> c = k; c != null; c = c.getSuperclass()) {
            if (c == Object.class) {
                return false;
            }
            for (Method m : c.getDeclaredMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == arity
                        && !m.isSynthetic()) {
                    return true;
                }
            }
        }
        return false;
    }
}
