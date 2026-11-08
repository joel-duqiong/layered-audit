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

class GenericFileHarnessValues extends GenericFileHarnessBase {
    // ---- 方法 ----
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
                // 依赖图里的类也认 ctor 规格 —— 否则嵌套依赖的构造参数只能靠猜
                JsonNode spec = ctorSpec(t.getName(), i);
                vals[i] = spec.isMissingNode() ? dependency(pt[i], depth + 1, path)
                        : buildValue(spec, pt[i], new Object[0]);
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
        spec = expandTemplates(spec);
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
            List<Constructor<?>> cands = new ArrayList<>();
            for (Constructor<?> cand : k.getDeclaredConstructors()) {
                if (cand.getParameterCount() == ctorArgs.size()) {
                    cands.add(cand);
                }
            }
            if (cands.isEmpty()) {
                throw new IllegalArgumentException("找不到匹配的构造器: " + k.getName());
            }
            // 同参数个数的构造器可能有多个（比如 (String, HttpInputMessage) 与 (String, Throwable)），
            // 挑参数类型真能装下这些实参的那个，别靠声明顺序赌。
            for (Constructor<?> c : cands) {
                Class<?>[] pt = c.getParameterTypes();
                Object[] vals = new Object[pt.length];
                boolean ok = true;
                for (int i = 0; i < pt.length; i++) {
                    try {
                        vals[i] = decode(ctorArgs.get(i), pt[i]);
                    } catch (Exception e) {
                        ok = false;
                        break;
                    }
                    if (vals[i] != null && !pt[i].isInstance(vals[i])) {
                        ok = false;
                        break;
                    }
                }
                if (!ok) {
                    continue;
                }
                c.setAccessible(true);
                return c.newInstance(vals);
            }
            throw new IllegalArgumentException("没有能装下这些参数的构造器: " + k.getName());
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

    static Object decode(JsonNode a, Class<?> type) throws Exception {
        if (a == null || a.isMissingNode() || a.isNull()) {
            return type == Optional.class ? Optional.empty() : null;
        }
        a = expandTemplates(a);
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
                    a.path("v").fields().forEachRemaining(e -> m.put(e.getKey(), plain(e.getValue())));
                    return m;
                }
                case "captured" -> {
                    return CAPTURED.get(a.path("v").asText());
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
            return methodParameterOf(Class.forName(a.path("type").asText()));
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

}
