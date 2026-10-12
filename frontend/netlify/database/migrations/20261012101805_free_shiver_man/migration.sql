CREATE TYPE "production_batch_status" AS ENUM('CREATED', 'IN_PROGRESS', 'TRANSFERRED', 'RECEIVED', 'COMPLETED', 'DISPUTED', 'CLOSED');--> statement-breakpoint
CREATE TYPE "production_loss_reason" AS ENUM('NATURAL_MORTALITY', 'DISEASE', 'INJURY', 'TECHNICAL_LOSS', 'OTHER');--> statement-breakpoint
CREATE TYPE "production_review_status" AS ENUM('PENDING', 'APPROVED', 'REJECTED');--> statement-breakpoint
CREATE TYPE "production_role" AS ENUM('OPERATOR', 'SHIFT_LEAD', 'MANAGER', 'ADMIN');--> statement-breakpoint
CREATE TYPE "production_stage" AS ENUM('INCUBATION', 'GROWING', 'SLAUGHTER', 'PACKAGING');--> statement-breakpoint
CREATE TYPE "production_transfer_status" AS ENUM('DRAFT', 'SENT', 'RECEIVED', 'DISPUTED', 'CONFIRMED', 'CANCELLED');--> statement-breakpoint
CREATE TABLE "production_balance_adjustments" (
	"id" serial PRIMARY KEY,
	"batch_id" integer NOT NULL,
	"stage" "production_stage" NOT NULL,
	"quantity_delta" integer NOT NULL,
	"reason" text NOT NULL,
	"status" "production_review_status" DEFAULT 'PENDING'::"production_review_status" NOT NULL,
	"created_by" integer NOT NULL,
	"approved_by" integer,
	"approved_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "production_adjustment_nonzero" CHECK ("quantity_delta" <> 0)
);
--> statement-breakpoint
CREATE TABLE "production_audit_logs" (
	"id" serial PRIMARY KEY,
	"actor_id" integer,
	"action" varchar(80) NOT NULL,
	"object_type" varchar(40) NOT NULL,
	"object_id" varchar(80) NOT NULL,
	"old_value" jsonb,
	"new_value" jsonb,
	"reason" text,
	"request_id" varchar(64),
	"previous_hash" varchar(64) NOT NULL,
	"event_hash" varchar(64) NOT NULL UNIQUE,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "production_batches" (
	"id" serial PRIMARY KEY,
	"batch_number" varchar(40) NOT NULL UNIQUE,
	"stage" "production_stage" NOT NULL,
	"initial_quantity" integer NOT NULL,
	"status" "production_batch_status" DEFAULT 'IN_PROGRESS'::"production_batch_status" NOT NULL,
	"notes" text,
	"created_by" integer NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "production_batch_positive_quantity" CHECK ("initial_quantity" > 0)
);
--> statement-breakpoint
CREATE TABLE "production_losses" (
	"id" serial PRIMARY KEY,
	"batch_id" integer NOT NULL,
	"stage" "production_stage" NOT NULL,
	"quantity" integer NOT NULL,
	"reason" "production_loss_reason" NOT NULL,
	"comment" text,
	"status" "production_review_status" DEFAULT 'PENDING'::"production_review_status" NOT NULL,
	"created_by" integer NOT NULL,
	"approved_by" integer,
	"approved_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "production_loss_positive" CHECK ("quantity" > 0)
);
--> statement-breakpoint
CREATE TABLE "production_notifications" (
	"id" serial PRIMARY KEY,
	"user_id" integer NOT NULL,
	"title" text NOT NULL,
	"body" text NOT NULL,
	"kind" varchar(40) NOT NULL,
	"object_type" varchar(40),
	"object_id" integer,
	"read_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
--> statement-breakpoint
CREATE TABLE "production_transfers" (
	"id" serial PRIMARY KEY,
	"transfer_number" varchar(40) NOT NULL UNIQUE,
	"batch_id" integer NOT NULL,
	"from_stage" "production_stage" NOT NULL,
	"to_stage" "production_stage" NOT NULL,
	"sent_quantity" integer NOT NULL,
	"received_quantity" integer,
	"discrepancy" integer,
	"status" "production_transfer_status" DEFAULT 'DRAFT'::"production_transfer_status" NOT NULL,
	"sender_id" integer NOT NULL,
	"receiver_id" integer,
	"confirmed_by" integer,
	"confirmation_comment" text,
	"rejection_reason" text,
	"sent_at" timestamp with time zone,
	"received_at" timestamp with time zone,
	"confirmed_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "production_transfer_positive_sent" CHECK ("sent_quantity" > 0),
	CONSTRAINT "production_transfer_received_range" CHECK ("received_quantity" IS NULL OR ("received_quantity" >= 0 AND "received_quantity" <= "sent_quantity")),
	CONSTRAINT "production_transfer_distinct_stages" CHECK ("from_stage" <> "to_stage")
);
--> statement-breakpoint
CREATE TABLE "production_users" (
	"id" serial PRIMARY KEY,
	"identity_id" uuid NOT NULL UNIQUE,
	"username" varchar(254) NOT NULL UNIQUE,
	"full_name" varchar(160) NOT NULL,
	"role" "production_role" NOT NULL,
	"stage" "production_stage",
	"is_active" boolean DEFAULT true NOT NULL,
	"must_change_password" boolean DEFAULT false NOT NULL,
	"last_login_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "production_user_stage_role" CHECK (("role" IN ('OPERATOR', 'SHIFT_LEAD') AND "stage" IS NOT NULL) OR ("role" IN ('MANAGER', 'ADMIN') AND "stage" IS NULL))
);
--> statement-breakpoint
CREATE INDEX "production_adjustment_batch" ON "production_balance_adjustments" ("batch_id");--> statement-breakpoint
CREATE INDEX "production_audit_created" ON "production_audit_logs" ("created_at");--> statement-breakpoint
CREATE INDEX "production_batch_stage" ON "production_batches" ("stage");--> statement-breakpoint
CREATE INDEX "production_loss_batch" ON "production_losses" ("batch_id");--> statement-breakpoint
CREATE INDEX "production_notification_user" ON "production_notifications" ("user_id");--> statement-breakpoint
CREATE INDEX "production_transfer_batch" ON "production_transfers" ("batch_id");--> statement-breakpoint
CREATE INDEX "production_transfer_stage_status" ON "production_transfers" ("from_stage","to_stage","status");--> statement-breakpoint
ALTER TABLE "production_balance_adjustments" ADD CONSTRAINT "production_balance_adjustments_hfzSBnLqCM8r_fkey" FOREIGN KEY ("batch_id") REFERENCES "production_batches"("id");--> statement-breakpoint
ALTER TABLE "production_balance_adjustments" ADD CONSTRAINT "production_balance_adjustments_DC82YVOAM91x_fkey" FOREIGN KEY ("created_by") REFERENCES "production_users"("id");--> statement-breakpoint
ALTER TABLE "production_balance_adjustments" ADD CONSTRAINT "production_balance_adjustments_ug88v21eArjm_fkey" FOREIGN KEY ("approved_by") REFERENCES "production_users"("id");--> statement-breakpoint
ALTER TABLE "production_audit_logs" ADD CONSTRAINT "production_audit_logs_actor_id_production_users_id_fkey" FOREIGN KEY ("actor_id") REFERENCES "production_users"("id");--> statement-breakpoint
ALTER TABLE "production_batches" ADD CONSTRAINT "production_batches_created_by_production_users_id_fkey" FOREIGN KEY ("created_by") REFERENCES "production_users"("id");--> statement-breakpoint
ALTER TABLE "production_losses" ADD CONSTRAINT "production_losses_batch_id_production_batches_id_fkey" FOREIGN KEY ("batch_id") REFERENCES "production_batches"("id");--> statement-breakpoint
ALTER TABLE "production_losses" ADD CONSTRAINT "production_losses_created_by_production_users_id_fkey" FOREIGN KEY ("created_by") REFERENCES "production_users"("id");--> statement-breakpoint
ALTER TABLE "production_losses" ADD CONSTRAINT "production_losses_approved_by_production_users_id_fkey" FOREIGN KEY ("approved_by") REFERENCES "production_users"("id");--> statement-breakpoint
ALTER TABLE "production_notifications" ADD CONSTRAINT "production_notifications_user_id_production_users_id_fkey" FOREIGN KEY ("user_id") REFERENCES "production_users"("id");--> statement-breakpoint
ALTER TABLE "production_transfers" ADD CONSTRAINT "production_transfers_batch_id_production_batches_id_fkey" FOREIGN KEY ("batch_id") REFERENCES "production_batches"("id");--> statement-breakpoint
ALTER TABLE "production_transfers" ADD CONSTRAINT "production_transfers_sender_id_production_users_id_fkey" FOREIGN KEY ("sender_id") REFERENCES "production_users"("id");--> statement-breakpoint
ALTER TABLE "production_transfers" ADD CONSTRAINT "production_transfers_receiver_id_production_users_id_fkey" FOREIGN KEY ("receiver_id") REFERENCES "production_users"("id");--> statement-breakpoint
ALTER TABLE "production_transfers" ADD CONSTRAINT "production_transfers_confirmed_by_production_users_id_fkey" FOREIGN KEY ("confirmed_by") REFERENCES "production_users"("id");