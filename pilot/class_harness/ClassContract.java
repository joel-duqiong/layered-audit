import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.lang.annotation.Annotation;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;
import java.lang.reflect.RecordComponent;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 类层静态侧：用反射把「实体声明」与「DTO 声明」精确导出成 JSON。
 * 不用源码正则 —— 正则分不清 record 组件上的约束注解到底落在字段还是访问器上，
 * 而这正是三层差分要对比的东西。
 */
public class ClassContract {

    static final ObjectMapper M = new ObjectMapper();

    public static void main(String[] args) throws Exception {
        List<String> names = new ArrayList<>();
        if (args.length > 0) {
            for (String cn : args) names.add(cn);
        } else {
            // 无参数时从 stdin 读类名列表（每行一个）
            java.io.BufferedReader in = new java.io.BufferedReader(new java.io.InputStreamReader(System.in));
            String line;
            while ((line = in.readLine()) != null) {
                line = line.trim();
                if (!line.isEmpty()) names.add(line);
            }
        }
        ArrayNode root = M.createArrayNode();
        for (String cn : names) {
            root.add(describe(Class.forName(cn)));
        }
        System.out.println(M.writeValueAsString(root));
    }

    static ObjectNode describe(Class<?> c) throws Exception {
        ObjectNode o = M.createObjectNode();
        o.put("class", c.getName());
        o.put("simple_name", c.getSimpleName());
        o.put("kind", c.isRecord() ? "record" : (c.isEnum() ? "enum" : "class"));
        o.put("super", c.getSuperclass() == null ? null : c.getSuperclass().getName());
        List<String> ifs = new ArrayList<>();
        for (Class<?> i : c.getInterfaces()) ifs.add(i.getName());
        o.set("interfaces", M.valueToTree(ifs));

        ArrayNode fields = M.createArrayNode();
        for (Field f : c.getDeclaredFields()) {
            if (f.isSynthetic()) continue;
            ObjectNode fo = M.createObjectNode();
            fo.put("name", f.getName());
            fo.put("type", f.getType().getName());
            fo.put("mods", Modifier.toString(f.getModifiers()));
            fo.set("annotations", dump(f.getAnnotations()));
            fields.add(fo);
        }
        o.set("fields", fields);

        if (c.isRecord()) {
            ArrayNode comps = M.createArrayNode();
            for (RecordComponent rc : c.getRecordComponents()) {
                ObjectNode co = M.createObjectNode();
                co.put("name", rc.getName());
                co.put("type", rc.getType().getName());
                // 约束注解会被 JSR380 传播到字段/访问器/构造参数三处，合并去重
                Map<String, Object> merged = new LinkedHashMap<>();
                Field f = null;
                try { f = c.getDeclaredField(rc.getName()); } catch (NoSuchFieldException ignored) { }
                if (f != null) mergeInto(merged, f.getAnnotations());
                mergeInto(merged, rc.getAccessor().getAnnotations());
                co.set("annotations", M.valueToTree(merged));
                comps.add(co);
            }
            o.set("components", comps);
        }
        return o;
    }

    static void mergeInto(Map<String, Object> target, Annotation[] anns) {
        for (Annotation a : anns) {
            target.put(a.annotationType().getSimpleName(), attrs(a));
        }
    }

    static ObjectNode dump(Annotation[] anns) {
        ObjectNode o = M.createObjectNode();
        for (Annotation a : anns) {
            o.set(a.annotationType().getSimpleName(), attrs(a));
        }
        return o;
    }

    static ObjectNode attrs(Annotation a) {
        ObjectNode o = M.createObjectNode();
        for (Method m : a.annotationType().getDeclaredMethods()) {
            if (m.getParameterCount() != 0) continue;
            try {
                Object v = m.invoke(a);
                if (v instanceof Object[] arr) {
                    List<String> l = new ArrayList<>();
                    for (Object x : arr) l.add(String.valueOf(x));
                    o.set(m.getName(), M.valueToTree(l));
                } else {
                    o.set(m.getName(), M.valueToTree(String.valueOf(v)));
                }
            } catch (Exception ignored) { }
        }
        return o;
    }
}
