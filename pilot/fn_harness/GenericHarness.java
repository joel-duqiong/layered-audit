import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.SerializationFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.web.multipart.MultipartFile;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 函数层执行载体（**通用版**）：在同一个 JVM 内直接反射调用被测类的私有方法。
 *
 * 目标类、依赖、打桩值、用例、类型别名全部来自**用例文件**（项目配置），
 * 本文件不含任何项目字面量 —— 换项目只换用例文件，不改编本文件。
 *
 * 用例文件结构：
 * {
 *   "types":    {"order": "com.x.Order", ...},       类型别名（$t 用它）
 *   "fixtures": {"11": {"$entity": "...", "fields": {...}}},   固定样本
 *   "stubs":    {"com.x.YRepo": {"method": <值规格>}},  仓库打桩
 *   "ctor":     {"com.x.ZService": ["<UPLOAD_ROOT>"]}, 构造器参数覆盖
 *   "cases":    [ ... ]
 * }
 *
 * 值规格（stubs）：
 *   {"$const": v}                    常量
 *   {"$echoArg0": true}               返回第一个入参（save 这类）
 *   {"$entity": fqcn, "fields": {...}} 反射构造实体
 *   {"$fixture": "11"}                取固定样本
 *   {"$byKey": {"<入参>": <规格>}}      按第一个入参取值
 *   {"$list": [规格...]}               列表
 *   {"$optional": 规格} / {"$optionalEmpty": true}
 *   {"$takenEcho": true}               第一个入参在「已占用集合」里则 true
 *   {"$countCalls": true}              记录调用次数
 *   {"$constAfterCalls": {"n": k, "value": v}}  前 k 次默认，之后返回 v
 */
public class GenericHarness {

    // 注册全部自动发现的模块：jsr310（时间→ISO 串）、parameter-names（record 反序列化）
    static final ObjectMapper M = new ObjectMapper().findAndRegisterModules()
            .disable(SerializationFeature.WRITE_DATES_AS_TIMESTAMPS);
    static JsonNode STUBS;
    static JsonNode FIXTURES;
    static JsonNode CTOR_ARGS;
    static JsonNode TYPES;
    static final Map<String, AtomicInteger> CALLS = new ConcurrentHashMap<>();
    static final Set<String> TAKEN = ConcurrentHashMap.newKeySet();
    static Path uploadRoot;

    public static void main(String[] args) throws Exception {
        uploadRoot = Paths.get(args[2]);
        Files.createDirectories(uploadRoot);
        JsonNode root = M.readTree(Paths.get(args[0]).toFile());
        STUBS = root.path("stubs");
        FIXTURES = root.path("fixtures");
        CTOR_ARGS = root.path("ctor");
        TYPES = root.path("types");
        ArrayNode out = M.createArrayNode();
        for (JsonNode c : root.path("cases")) {
            out.add(runCase(c));
        }
        Files.writeString(Paths.get(args[1]), out.toString());
    }

    // ---------------------------------------------------------------- 用例执行

    static JsonNode runCase(JsonNode c) {
        ObjectNode r = M.createObjectNode();
        String id = c.path("id").asText();
        String func = c.path("func").asText();
        r.put("id", id);
        r.put("func", func);
        r.put("label", c.path("label").asText(""));
        r.put("target", c.path("class").asText("") + "#" + c.path("method").asText(""));
        TAKEN.clear();
        CALLS.clear();
        // 用例级打桩覆盖全局打桩（同一个仓储在不同用例里行为不同）
        JsonNode own = c.path("stubs");
        CASE_STUBS = (own.isObject() && own.size() > 0) ? own : null;
        try {
            if (func.equals("burst")) {
                runBurst(c, r);
            } else if (func.equals("conflict")) {
                runConflict(c, r);
            } else {
                runReflect(c, r);
            }
        } catch (InvocationTargetException e) {
            recordThrowable(r, e.getCause());
        } catch (Throwable t) {
            recordThrowable(r, t);
        }
        ObjectNode counts = M.createObjectNode();
        CALLS.forEach((k, v) -> counts.put(k, v.get()));
        r.set("call_counts", counts);
        return r;
    }

    static void recordThrowable(ObjectNode r, Throwable cause) {
        r.put("outcome", cause instanceof InvocationTargetException ? "error" : "threw");
        r.put("exception_class", cause.getClass().getName());
        r.put("exception_message", String.valueOf(cause.getMessage()));
        try {
            Method getCode = cause.getClass().getMethod("getErrorCode");
            r.put("business_error_code", String.valueOf(getCode.invoke(cause)));
        } catch (Exception ignored) {
            // 不是业务异常
        }
        try {
            Method getStatus = cause.getClass().getMethod("getStatus");
            Object st = getStatus.invoke(cause);
            if (st != null) {
                Method value = st.getClass().getMethod("value");
                Object v = value.invoke(st);
                r.set("http_status", M.getNodeFactory().numberNode(((Number) v).intValue()));
            }
        } catch (Exception ignored) {
            // 没有状态码
        }
    }

    static void runReflect(JsonNode c, ObjectNode r) throws Exception {
        String cls = c.path("class").asText();
        String methodName = c.path("method").asText();
        Object target = newTarget(cls);
        JsonNode args = c.path("args");
        Method method = findMethod(target.getClass(), methodName, args.size());
        method.setAccessible(true);
        Class<?>[] types = method.getParameterTypes();
        Object[] argv = new Object[args.size()];
        for (int i = 0; i < argv.length; i++) {
            argv[i] = decode(args.get(i), types[i], method.getGenericParameterTypes()[i]);
        }
        if (methodName.toLowerCase().contains("upload") && uploadRoot != null) {
            cleanUploadRoot();
        }
        long t0 = System.nanoTime();
        Object v;
        try {
            v = method.invoke(target, argv);
        } catch (IllegalArgumentException ia) {
            StringBuilder sb = new StringBuilder("argument type mismatch: ");
            for (int i = 0; i < argv.length; i++) {
                sb.append(i).append(" want=").append(types[i].getName())
                  .append(" got=").append(argv[i] == null ? "null" : argv[i].getClass().getName())
                  .append("; ");
            }
            throw new IllegalArgumentException(sb.toString(), ia);
        }
        long t1 = System.nanoTime();
        r.put("outcome", "returned");
        r.set("value", toJson(v));
        r.put("duration_us", (t1 - t0) / 1000);
        // 领域对象的状态：入参优先，没有就取 receiver（实例方法里 receiver 才是被观察的对象）
        Object observed = null;
        if (argv.length > 0 && argv[0] != null && isDomain(argv[0])) {
            observed = argv[0];
        } else if (target != null && isDomain(target)) {
            observed = target;
        }
        if (observed != null) {
            r.set("entity_state", entityState(observed));
        }
        if (methodName.toLowerCase().contains("upload")) {
            r.set("upload_dir_state", uploadState());
        }
    }

    /** 并发调用同一工厂方法 n 次，检查结果唯一性。 */
    static void runBurst(JsonNode c, ObjectNode r) throws Exception {
        int n = c.path("n").asInt(2000);
        int threads = c.path("threads").asInt(16);
        String cls = c.path("class").asText();
        Object target = newTarget(cls);
        Method m = findMethod(target.getClass(), c.path("method").asText(), 0);
        m.setAccessible(true);
        ExecutorService pool = Executors.newFixedThreadPool(Math.max(1, threads));
        CountDownLatch start = new CountDownLatch(1);
        List<Future<Object>> futures = new ArrayList<>();
        for (int i = 0; i < n; i++) {
            futures.add(pool.submit(() -> {
                start.await();
                return m.invoke(target);
            }));
        }
        start.countDown();
        List<Object> values = new ArrayList<>();
        for (Future<Object> f : futures) {
            values.add(f.get());
        }
        pool.shutdown();
        Set<Object> distinct = ConcurrentHashMap.newKeySet();
        int duplicates = 0;
        for (Object v : values) {
            if (!distinct.add(v)) {
                duplicates++;
            }
        }
        r.put("outcome", "returned");
        r.put("samples", values.size());
        r.put("generated", values.size());
        r.put("distinct", distinct.size());
        r.put("duplicates", duplicates);
        r.put("malformed", values.size() - distinct.size() - duplicates);
        r.put("elapsed_ms", 0);
        r.set("values_sample", M.valueToTree(values.subList(0, Math.min(20, values.size()))));
    }

    /** 冲突重试：第一次调用把结果占住，第二次应触发「已存在」并重试。 */
    static void runConflict(JsonNode c, ObjectNode r) throws Exception {
        String cls = c.path("class").asText();
        Object target = newTarget(cls);
        Method m = findMethod(target.getClass(), c.path("method").asText(), 0);
        m.setAccessible(true);
        try {
            Object v = m.invoke(target);
            r.put("outcome", "returned");
            r.set("value", toJson(v));
        } catch (InvocationTargetException e) {
            recordThrowable(r, e.getCause());
        } catch (Throwable t) {
            recordThrowable(r, t);
        }
    }

    static void cleanUploadRoot() {
        try (var stream = Files.walk(uploadRoot)) {
            stream.filter(Files::isRegularFile).forEach(f -> {
                try {
                    Files.deleteIfExists(f);
                } catch (IOException ignored) {
                    // 忽略
                }
            });
        } catch (IOException ignored) {
            // 忽略
        }
    }

    static Method findMethod(Class<?> type, String name, int arity) throws NoSuchMethodException {
        for (Method m : type.getDeclaredMethods()) {
            if (m.getName().equals(name) && m.getParameterCount() == arity) {
                return m;
            }
        }
        for (Class<?> c = type.getSuperclass(); c != null; c = c.getSuperclass()) {
            for (Method m : c.getDeclaredMethods()) {
                if (m.getName().equals(name) && m.getParameterCount() == arity) {
                    return m;
                }
            }
        }
        throw new NoSuchMethodException(type.getName() + "#" + name + "/" + arity);
    }

    // ---------------------------------------------------------------- 反射构造

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
        JsonNode overrides = CTOR_ARGS.path(cls);
        Class<?>[] types = best.getParameterTypes();
        Object[] deps = new Object[types.length];
        for (int i = 0; i < deps.length; i++) {
            JsonNode spec = overrides.path(String.valueOf(i));
            deps[i] = spec.isMissingNode() ? dependency(types[i]) : ctorValue(spec, types[i]);
        }
        best.setAccessible(true);
        return best.newInstance(deps);
    }

    /** 构造器参数覆盖值：能构造成目标类型就构造，构造不了再按类型猜。 */
    static Object ctorValue(JsonNode spec, Class<?> type) {
        String text = spec.asText();
        if ("<UPLOAD_ROOT>".equals(text)) {
            return uploadRoot == null ? "" : uploadRoot.toString();
        }
        if (type == String.class) {
            return text;
        }
        // 目标类型有一个「单 String 构造器」时，用这个值构造出来
        // （例：FxClient(String baseUrl)）
        for (Constructor<?> c : type.getDeclaredConstructors()) {
            Class<?>[] pt = c.getParameterTypes();
            if (pt.length == 1 && (pt[0] == String.class || pt[0] == Object.class)) {
                try {
                    c.setAccessible(true);
                    return c.newInstance(pt[0] == String.class ? text : null);
                } catch (Exception ignored) {
                    // 换下一个构造器
                }
            }
        }
        return dependency(type);
    }

    /**
     * 依赖注入（通用）：像容器一样递归构造依赖图。
     *   接口 -> 动态代理（查打桩表）
     *   ObjectMapper -> 真实例；String -> ""；基本类型 -> 0/false
     *   具体类 -> 递归构造它的构造器参数（深度上限防环）
     * 这样 PricingService(PricingStrategy) 这种「具体类依赖接口」的组合能自动满足。
     */
    static Object dependency(Class<?> t) {
        return dependency(t, 0, new java.util.ArrayList<>());
    }

    static Object dependency(Class<?> t, int depth, List<Class<?>> path) {
        if (t == ObjectMapper.class) {
            return M;
        }
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
            String key = iface.getSimpleName() + "#" + m.getName();
            CALLS.computeIfAbsent(key, k -> new AtomicInteger()).incrementAndGet();
            JsonNode spec = stubFor(iface, m.getName());
            if (spec == null) {
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
                if (CALLS.get(key).get() <= n) {
                    return defaultValue(m.getReturnType());
                }
                return buildValue(spec.path("$constAfterCalls").path("value"), m.getReturnType(), a);
            }
            if (spec.has("$takenEcho")) {
                return a != null && a.length > 0 && TAKEN.contains(String.valueOf(a[0]));
            }
            return buildValue(spec, m.getReturnType(), a);
        };
        return Proxy.newProxyInstance(GenericHarness.class.getClassLoader(),
                new Class<?>[]{iface}, h);
    }

    static JsonNode CASE_STUBS;

    /** 打桩查找：先用例级（覆盖全局），再全局。接口名两种写法都认（全限定 / 简单名）。 */
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
        if (spec.has("$const")) {
            return coerce(spec.path("$const"), type);
        }
        if (spec.has("$echoArg0")) {
            return args != null && args.length > 0 ? args[0] : defaultValue(type);
        }
        if (spec.has("$entity")) {
            Object e = buildEntity(spec.path("$entity").asText(), spec.path("fields"));
            return type == Optional.class ? Optional.ofNullable(e) : e;
        }
        if (spec.has("$fixture")) {
            JsonNode fx = FIXTURES.path(spec.path("$fixture").asText());
            if (fx.isMissingNode()) {
                return defaultValue(type);
            }
            return buildValue(fx, type, args);
        }
        if (spec.has("$byKey")) {
            String k = args != null && args.length > 0 ? String.valueOf(args[0]) : "";
            JsonNode inner = spec.path("$byKey").path(k);
            if (inner.isMissingNode()) {
                return defaultValue(type);
            }
            return buildValue(inner, type, args);
        }
        if (spec.has("$list")) {
            List<Object> out = new ArrayList<>();
            for (JsonNode item : spec.path("$list")) {
                out.add(buildValue(item, Object.class, args));
            }
            return out;
        }
        if (spec.has("$optional")) {
            return Optional.ofNullable(buildValue(spec.path("$optional"), Object.class, args));
        }
        if (spec.has("$optionalEmpty")) {
            return Optional.empty();
        }
        return coerce(spec, type);
    }

    /** 反射构造实体：按字段名找 setter，逐个赋值。 */
    static Object buildEntity(String fqcn, JsonNode fields) throws Exception {
        Class<?> k = Class.forName(fqcn);
        if (fields != null && !fields.isEmpty()) {
            // 优先用 Jackson 反序列化 —— record 没有无参构造器，Jackson 能直接还原
            try {
                return M.treeToValue(fields, k);
            } catch (Exception ignored) {
                // 落到 setter 方式
            }
        }
        Object o = k.getDeclaredConstructor().newInstance();
        if (fields == null) {
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

    /** 领域对象：不是基本类型/String/枚举/JDK 类型。 */
    static boolean isDomain(Object o) {
        Class<?> k = o.getClass();
        return !isSimple(k) && !k.getName().startsWith("java.") && !k.getName().startsWith("jdk.");
    }

    static boolean isSimple(Class<?> k) {
        return k.isPrimitive() || k == String.class || k == Integer.class || k == Long.class
                || k == Boolean.class || k == Double.class || k.isEnum();
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
        if (List.class.isAssignableFrom(type)) {
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

    static Object defaultValue(Class<?> t) {
        if (!t.isPrimitive()) {
            return Optional.empty();
        }
        if (t == boolean.class) {
            return Boolean.FALSE;
        }
        if (t == int.class) {
            return 0;
        }
        if (t == long.class) {
            return 0L;
        }
        if (t == double.class) {
            return 0d;
        }
        if (t == float.class) {
            return 0f;
        }
        if (t == short.class) {
            return (short) 0;
        }
        if (t == byte.class) {
            return (byte) 0;
        }
        if (t == char.class) {
            return (char) 0;
        }
        return null;
    }

    // ---------------------------------------------------------------- 参数解码

    static Object decode(JsonNode a, Class<?> type, java.lang.reflect.Type generic) throws Exception {
        if (a == null || a.isMissingNode() || a.isNull()) {
            return null;
        }
        if (a.isObject() && a.has("$t")) {
            String t = a.get("$t").asText();
            switch (t) {
                case "null":
                    return null;
                case "long":
                    return a.path("v").asLong();
                case "int":
                    return a.path("v").asInt();
                case "str":
                    return a.path("v").asText();
                case "bool":
                    return a.path("v").asBoolean();
                case "dt":
                    return OffsetDateTime.parse(a.path("v").asText());
                case "enum":
                    return enumValue(a, type);
                case "order":
                    return newEntity(typeAlias(a, "order"));
                case "entity":
                    return buildEntity(a.path("class").asText(), a.path("fields"));
                case "fixture":
                    return buildValue(FIXTURES.path(a.path("v").asText()), Object.class, new Object[0]);
                case "pkg":
                case "product":
                    return buildValue(FIXTURES.path(a.path("v").asText()), Object.class, new Object[0]);
                case "pkgItems":
                case "addItems":
                    return decodeItemList(a.path("v"), typeAlias(a, t));
                case "categoryReq":
                    return constructByName(typeAlias(a, "categoryReq"),
                            a.path("v").isNull() ? M.getNodeFactory().nullNode()
                                    : M.getNodeFactory().textNode(a.path("v").asText()));
                case "multipart":
                    return decodeMultipart(a.path("v"));
                case "bytes":
                    return Base64.getDecoder().decode(a.path("v").asText());
                default:
                    throw new IllegalArgumentException("unknown arg type " + t);
            }
        }
        if (List.class.isAssignableFrom(type)) {
            List<Object> out = new ArrayList<>();
            for (JsonNode el : a) {
                out.add(decode(el, Object.class, Object.class));
            }
            return out;
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
        try {
            return M.treeToValue(a, type);
        } catch (Exception e) {
            throw new IllegalArgumentException("cannot decode " + a + " as " + type, e);
        }
    }

    /** 类型别名：$t 里写的名字 -> 用例文件 types 段里的全限定名。 */
    static String typeAlias(JsonNode a, String key) {
        String alias = a.path("type").asText(key);
        String fqcn = TYPES.path(alias).asText("");
        if (fqcn.isEmpty()) {
            throw new IllegalArgumentException("用例文件里没有类型别名: " + alias);
        }
        return fqcn;
    }

    static Object newEntity(String fqcn) {
        try {
            return Class.forName(fqcn).getDeclaredConstructor().newInstance();
        } catch (Exception e) {
            throw new IllegalStateException("构造实体失败: " + fqcn, e);
        }
    }

    static Object enumValue(JsonNode a, Class<?> type) {
        String cls = a.path("class").asText("");
        if (cls.isEmpty()) {
            cls = TYPES.path("enum").asText("");
        }
        String name = a.path("v").asText();
        Class<?> k = type;
        if (!cls.isEmpty()) {
            try {
                k = Class.forName(cls);
            } catch (ClassNotFoundException ignored) {
                // 用入参类型
            }
        }
        if (k != null && k.isEnum()) {
            for (Object c : k.getEnumConstants()) {
                if (c.toString().equals(name)) {
                    return c;
                }
            }
        }
        throw new IllegalArgumentException("枚举值解析不了: " + name);
    }

    static List<Object> decodeItemList(JsonNode arr, String fqcn) throws Exception {
        List<Object> out = new ArrayList<>();
        if (arr == null) {
            return out;
        }
        for (JsonNode el : arr) {
            Object e;
            try {
                // 优先 Jackson（record / 有构造器的 DTO 都能还原）
                e = M.treeToValue(el, Class.forName(fqcn));
            } catch (Exception ex) {
                e = Class.forName(fqcn).getDeclaredConstructor().newInstance();
            }
            for (var f = el.fields(); f.hasNext();) {
                var e2 = f.next();
                Method setter = findSetter(e.getClass(), e2.getKey());
                if (setter == null) {
                    continue;
                }
                setter.setAccessible(true);
                setter.invoke(e, buildValue(e2.getValue(), setter.getParameterTypes()[0], new Object[0]));
            }
            out.add(e);
        }
        return out;
    }

    static Object constructByName(String fqcn, JsonNode arg) throws Exception {
        Class<?> k = Class.forName(fqcn);
        Constructor<?> best = null;
        for (Constructor<?> c : k.getDeclaredConstructors()) {
            if (best == null || c.getParameterCount() < best.getParameterCount()) {
                best = c;
            }
        }
        best.setAccessible(true);
        Class<?>[] types = best.getParameterTypes();
        Object[] vals = new Object[types.length];
        for (int i = 0; i < vals.length; i++) {
            vals[i] = arg == null || arg.isNull()
                    ? defaultValue(types[i])
                    : coerce(arg, types[i]);
        }
        return best.newInstance(vals);
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

    /** 反射兜底：record 用组件，POJO 用 getter（私有嵌套 record Jackson 序列化不了）。 */
    static JsonNode reflectiveDump(Object o) throws Exception {
        ObjectNode n = M.createObjectNode();
        Class<?> k = o.getClass();
        if (k.isRecord()) {
            for (var c : k.getRecordComponents()) {
                try {
                    Method acc = c.getAccessor();
                    acc.setAccessible(true);   // 私有嵌套 record 的访问器要放开
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
    static ObjectNode entityState(Object o) {
        ObjectNode n = M.createObjectNode();
        if (o == null) {
            return n;
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
                n.set(name, toJson(m.invoke(o)));
            } catch (Exception ignored) {
                // 读不到就跳过
            }
        }
        return n;
    }

    static ArrayNode uploadState() {
        ArrayNode n = M.createArrayNode();
        List<Object> files = new ArrayList<>();
        if (uploadRoot != null) {
            try (var stream = Files.walk(uploadRoot)) {
                stream.filter(Files::isRegularFile).forEach(f -> {
                    ObjectNode e = M.createObjectNode();
                    e.put("name", f.getFileName().toString());
                    try {
                        e.put("size", Files.size(f));
                    } catch (IOException ignored) {
                        e.put("size", 0);
                    }
                    files.add(e);
                });
            } catch (IOException ignored) {
                // 忽略
            }
        }
        for (Object f : files) {
            n.add(M.valueToTree(f));
        }
        return n;
    }

    // ---------------------------------------------------------------- 上传桩

    static class StubMultipart implements MultipartFile {
        private final String filename;
        private final String contentType;
        private final byte[] content;

        StubMultipart(String filename, String contentType, byte[] content) {
            this.filename = filename;
            this.contentType = contentType;
            this.content = content;
        }

        @Override
        public String getName() {
            return "file";
        }

        @Override
        public String getOriginalFilename() {
            return filename;
        }

        @Override
        public String getContentType() {
            return contentType;
        }

        @Override
        public boolean isEmpty() {
            return content.length == 0;
        }

        @Override
        public long getSize() {
            return content.length;
        }

        @Override
        public byte[] getBytes() {
            return content;
        }

        @Override
        public InputStream getInputStream() {
            return new ByteArrayInputStream(content);
        }

        @Override
        public void transferTo(java.io.File dest) throws IOException {
            Files.write(dest.toPath(), content);
        }
    }

    static MultipartFile decodeMultipart(JsonNode v) {
        String filename = v.path("filename").asText("upload.png");
        String contentType = v.path("contentType").asText("image/png");
        byte[] content;
        if (v.has("contentBase64")) {
            content = Base64.getDecoder().decode(v.path("contentBase64").asText());
        } else if (v.has("sizeBytes")) {
            content = new byte[(int) v.path("sizeBytes").asInt(0)];
        } else {
            content = new byte[0];
        }
        return new StubMultipart(filename, contentType, content);
    }
}
