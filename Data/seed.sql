-- 内置初始文献：三篇大模型（LLM）相关论文
--
-- 仅在「首次启动且库内没有任何论文」时由后端写入，
-- 见 backend/app/services/seed_service.py 的 seed_initial_library()。
--
-- 配套的 PDF 放在 Data/seed_pdfs/，播种时会像「刚上传 PDF」一样：
-- 复制进 workspace/storage/<论文id>/original.pdf、建立附件记录、触发后台分析。
--
-- 元数据来源：arXiv 论文页（作者、摘要、DOI 均照抄原文）。

-- ===== 1) Attention Is All You Need（Transformer 架构） =====
INSERT INTO papers (id, title, title_cn, title_en, authors, publish_date, abstract, source_url, status)
VALUES (
    'paper-demo-0001',
    'Attention Is All You Need',
    '注意力机制就是你所需要的',
    'Attention Is All You Need',
    'Ashish Vaswani; Noam Shazeer; Niki Parmar; Jakob Uszkoreit; Llion Jones; Aidan N. Gomez; Łukasz Kaiser; Illia Polosukhin',
    '2017-06-12',
    'The dominant sequence transduction models are based on complex recurrent or convolutional neural networks in an encoder-decoder configuration. The best performing models also connect the encoder and decoder through an attention mechanism. We propose a new simple network architecture, the Transformer, based solely on attention mechanisms, dispensing with recurrence and convolutions entirely. Experiments on two machine translation tasks show these models to be superior in quality while being more parallelizable and requiring significantly less time to train. Our model achieves 28.4 BLEU on the WMT 2014 English-to-German translation task, improving over the existing best results, including ensembles by over 2 BLEU. On the WMT 2014 English-to-French translation task, our model establishes a new single-model state-of-the-art BLEU score of 41.8 after training for 3.5 days on eight GPUs, a small fraction of the training costs of the best models from the literature. We show that the Transformer generalizes well to other tasks by applying it successfully to English constituency parsing both with large and limited training data.',
    'https://arxiv.org/abs/1706.03762',
    'uploaded'
);

INSERT INTO paper_texts (id, paper_id, text_scope, title_extracted, abstract_extracted, sections_json, parse_status)
VALUES (
    'text-demo-0001',
    'paper-demo-0001',
    'metadata',
    'Attention Is All You Need',
    'The dominant sequence transduction models are based on complex recurrent or convolutional neural networks in an encoder-decoder configuration.',
    '{"metadata": {"keywords": "Transformer, 自注意力, 序列建模, 机器翻译", "doi": "10.48550/arXiv.1706.03762", "year": "2017", "source": "arXiv"}}',
    'done'
);

-- ===== 2) BERT（双向预训练语言模型） =====
INSERT INTO papers (id, title, title_cn, title_en, authors, publish_date, abstract, source_url, status)
VALUES (
    'paper-demo-0002',
    'BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding',
    'BERT：面向语言理解的深度双向 Transformer 预训练',
    'BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding',
    'Jacob Devlin; Ming-Wei Chang; Kenton Lee; Kristina Toutanova',
    '2019-05-24',
    'We introduce a new language representation model called BERT, which stands for Bidirectional Encoder Representations from Transformers. Unlike recent language representation models, BERT is designed to pre-train deep bidirectional representations from unlabeled text by jointly conditioning on both left and right context in all layers. As a result, the pre-trained BERT model can be fine-tuned with just one additional output layer to create state-of-the-art models for a wide range of tasks, such as question answering and language inference, without substantial task-specific architecture modifications. BERT is conceptually simple and empirically powerful. It obtains new state-of-the-art results on eleven natural language processing tasks, including pushing the GLUE score to 80.5% (7.7% point absolute improvement), MultiNLI accuracy to 86.7% (4.6% absolute improvement), SQuAD v1.1 question answering Test F1 to 93.2 (1.5 point absolute improvement) and SQuAD v2.0 Test F1 to 83.1 (5.1 point absolute improvement).',
    'https://arxiv.org/abs/1810.04805',
    'uploaded'
);

INSERT INTO paper_texts (id, paper_id, text_scope, title_extracted, abstract_extracted, sections_json, parse_status)
VALUES (
    'text-demo-0002',
    'paper-demo-0002',
    'metadata',
    'BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding',
    'We introduce a new language representation model called BERT, which stands for Bidirectional Encoder Representations from Transformers.',
    '{"metadata": {"keywords": "BERT, 预训练, 双向编码器, 语言理解", "doi": "10.48550/arXiv.1810.04805", "year": "2019", "source": "arXiv"}}',
    'done'
);

-- ===== 3) GPT-3（大语言模型的少样本能力） =====
INSERT INTO papers (id, title, title_cn, title_en, authors, publish_date, abstract, source_url, status)
VALUES (
    'paper-demo-0003',
    'Language Models are Few-Shot Learners',
    '语言模型是小样本学习器',
    'Language Models are Few-Shot Learners',
    'Tom B. Brown; Benjamin Mann; Nick Ryder; Melanie Subbiah; Jared Kaplan; Prafulla Dhariwal; Arvind Neelakantan; Pranav Shyam; Girish Sastry; Amanda Askell; Sandhini Agarwal; Ariel Herbert-Voss; Gretchen Krueger; Tom Henighan; Rewon Child; Aditya Ramesh; Daniel M. Ziegler; Jeffrey Wu; Clemens Winter; Christopher Hesse; Mark Chen; Eric Sigler; Mateusz Litwin; Scott Gray; Benjamin Chess; Jack Clark; Christopher Berner; Sam McCandlish; Alec Radford; Ilya Sutskever; Dario Amodei',
    '2020-07-22',
    'Recent work has demonstrated substantial gains on many NLP tasks and benchmarks by pre-training on a large corpus of text followed by fine-tuning on a specific task. While typically task-agnostic in architecture, this method still requires task-specific fine-tuning datasets of thousands or tens of thousands of examples. By contrast, humans can generally perform a new language task from only a few examples or from simple instructions - something which current NLP systems still largely struggle to do. Here we show that scaling up language models greatly improves task-agnostic, few-shot performance, sometimes even reaching competitiveness with prior state-of-the-art fine-tuning approaches. Specifically, we train GPT-3, an autoregressive language model with 175 billion parameters, 10x more than any previous non-sparse language model, and test its performance in the few-shot setting. For all tasks, GPT-3 is applied without any gradient updates or fine-tuning, with tasks and few-shot demonstrations specified purely via text interaction with the model. GPT-3 achieves strong performance on many NLP datasets, including translation, question-answering, and cloze tasks, as well as several tasks that require on-the-fly reasoning or domain adaptation, such as unscrambling words, using a novel word in a sentence, or performing 3-digit arithmetic. At the same time, we also identify some datasets where GPT-3''s few-shot learning still struggles, as well as some datasets where GPT-3 faces methodological issues related to training on large web corpora. Finally, we find that GPT-3 can generate samples of news articles which human evaluators have difficulty distinguishing from articles written by humans. We discuss broader societal impacts of this finding and of GPT-3 in general.',
    'https://arxiv.org/abs/2005.14165',
    'uploaded'
);

INSERT INTO paper_texts (id, paper_id, text_scope, title_extracted, abstract_extracted, sections_json, parse_status)
VALUES (
    'text-demo-0003',
    'paper-demo-0003',
    'metadata',
    'Language Models are Few-Shot Learners',
    'Here we show that scaling up language models greatly improves task-agnostic, few-shot performance.',
    '{"metadata": {"keywords": "大语言模型, GPT-3, 少样本学习, 上下文学习", "doi": "10.48550/arXiv.2005.14165", "year": "2020", "source": "arXiv"}}',
    'done'
);

-- ===== 标签 =====
INSERT INTO tags (id, name, color) VALUES
    ('tag-demo-llm', '大语言模型', '#6C8EAD'),
    ('tag-demo-pretrain', '预训练', '#8FB3A0'),
    ('tag-demo-transformer', 'Transformer', '#B07AAC');

INSERT INTO paper_tags (paper_id, tag_id) VALUES
    ('paper-demo-0001', 'tag-demo-transformer'),
    ('paper-demo-0001', 'tag-demo-pretrain'),
    ('paper-demo-0002', 'tag-demo-pretrain'),
    ('paper-demo-0002', 'tag-demo-llm'),
    ('paper-demo-0003', 'tag-demo-llm');
