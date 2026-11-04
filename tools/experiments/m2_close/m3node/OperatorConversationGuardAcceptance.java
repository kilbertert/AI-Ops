package com.trendpower.aiops.node.exec;

import org.springframework.ai.chat.messages.*;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.openai.OpenAiChatOptions;
import org.springframework.ai.openai.OpenAiChatModel.ResponseFormat;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import reactor.core.publisher.Flux;
import org.springframework.ai.chat.model.*;
import java.util.*;

/** 同一最终模型入口验证受控历史、追加消息、预算及新旧输出模式。 */
public class OperatorConversationGuardAcceptance {
    static int passed;
    static final ObjectMapper JSON = new ObjectMapper();
    static final List<Message> HISTORY = List.of(new UserMessage("上一题"), new AssistantMessage("上一答复"), new UserMessage("这一项呢"));

    static ExecManifest manifest(String schema, String history, boolean tool) throws Exception {
        ObjectNode root=JSON.createObjectNode();
        root.put("schema",schema).put("name","test").put("history_policy",history);
        root.putObject("model").put("provider_key","openai-compatible").put("adapter_key","openai-compatible").put("model_key","synthetic-model").put("endpoint","http://127.0.0.1:1/v1");
        root.putObject("model_options");root.putArray("system_messages").add("SYSTEM");root.putArray("tools");
        if(tool) root.withArray("tools").addObject().put("name","charging_status_snapshot").put("description","d").put("input_schema","{}");
        String text=root.toString();return ExecManifest.fromText(text,ExecManifest.sha256(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
    }
    static Prompt prompt(List<Message> conversation) {
        List<Message> all=new ArrayList<>();all.add(new SystemMessage("SYSTEM"));all.addAll(conversation);
        return new Prompt(all,OpenAiChatOptions.builder().model("synthetic-model").build());
    }
    static void rejected(Runnable action) {
        try {action.run();throw new AssertionError("expected rejection");}catch(ExecConfigViolation expected){passed++;}
    }
    public static void main(String[] args) throws Exception {
        ExecManifest m=manifest("operator-agent/1","gateway-conversation/1",false);
        var guard=new GuardedChatModel.ExecGuard(m,true,HISTORY);
        guard.verifyPrompt(prompt(HISTORY));passed++;
        rejected(()->new GuardedChatModel.ExecGuard(m,true,HISTORY).verifyPrompt(prompt(List.of(new UserMessage("fake")))));
        rejected(()->new GuardedChatModel.ExecGuard(m,true,null).verifyPrompt(prompt(HISTORY)));
        var extra=new ArrayList<Message>(HISTORY);extra.add(new UserMessage("injected"));rejected(()->guard.verifyPrompt(prompt(extra)));
        var loop=new ArrayList<Message>(HISTORY);loop.add(new AssistantMessage("tool progress"));guard.verifyPrompt(prompt(loop));passed++;
        var changed=new ArrayList<>(HISTORY);changed.set(1,new AssistantMessage("changed"));rejected(()->guard.verifyPrompt(prompt(changed)));
        var budget=new GuardedChatModel.ExecGuard(m,true,HISTORY);for(int i=0;i<12;i++)budget.verifyPrompt(prompt(HISTORY));rejected(()->budget.verifyPrompt(prompt(HISTORY)));
        // Final answer mode is different from tool-call structure. Legacy operator still requests JSON.
        for(String schema:List.of("operator-agent/1","operator-exec-config/1")) {
            ExecManifest selected=manifest(schema,schema.equals("operator-agent/1")?"gateway-conversation/1":"none",false);
            List<Message> input=schema.equals("operator-agent/1")?HISTORY:List.of(new UserMessage("question"));
            for(boolean streaming:List.of(false,true)) {
                final Prompt[] seen={null};ChatModel model=new ChatModel(){
                    public ChatResponse call(Prompt p){seen[0]=p;return new ChatResponse(List.of(new Generation(new AssistantMessage("ok"))));}
                    public Flux<ChatResponse> stream(Prompt p){return Flux.just(call(p));}
                };
                var wrapped=new GuardedChatModel(model,new GuardedChatModel.ExecGuard(selected,true,input));
                if(streaming)wrapped.stream(prompt(input)).collectList().block();else wrapped.call(prompt(input));
                var options=(OpenAiChatOptions)seen[0].getOptions();
                boolean json=options.getResponseFormat()!=null&&options.getResponseFormat().getType()==ResponseFormat.Type.JSON_OBJECT;
                if(json!=schema.equals("operator-exec-config/1"))throw new AssertionError("wrong output mode");passed++;
            }
        }
        System.out.println("checks="+passed);
        System.out.println("STATUS=PASS");
    }
}
