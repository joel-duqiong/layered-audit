import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.lang.invoke.MethodHandle;
import java.lang.invoke.MethodHandles;
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

class GenericFileHarnessDetectors extends GenericFileHarnessValues {
    // ---- 方法 ----
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
                    invokeSuperImpl(sup, instA, argv);
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

    /**
     * 非虚调用父类实现。
     *
     * Method.invoke 是虚分派 —— 拿父类的 Method 去 invoke，仍然跳到子类的覆写，
     * 于是「父类行为」和「子类行为」永远一样，判据等于没写。
     * 这里用 MethodHandles 的 special 调用，拿到真正的父类实现。
     */
    static Object invokeSuperImpl(Method sup, Object inst, Object[] argv) throws Throwable {
        MethodHandles.Lookup lk = MethodHandles.privateLookupIn(
                sup.getDeclaringClass(), MethodHandles.lookup());
        MethodHandle mh = lk.unreflectSpecial(sup, sup.getDeclaringClass());
        Object[] args = new Object[argv.length + 1];
        args[0] = inst;
        System.arraycopy(argv, 0, args, 1, argv.length);
        return mh.invokeWithArguments(args);
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
                    Set<String> found = new LinkedHashSet<>();
                    found.addAll(ClassConstPool.urls(Files.readAllBytes(file)));
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
